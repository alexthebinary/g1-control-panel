#!/usr/bin/env python3
"""
g1_trigger_server.py — Web API for triggering G1 motion clips.

Runs on the robot. Exposes REST endpoints for the web control panel.

Endpoints:
  GET  /                  → Redirect to /docs
  GET  /api/list          → All available triggers
  GET  /api/status        → FSM state, uptime
  POST /api/trigger       → Trigger a motion by keyword  {"text": "charleston"}
  POST /api/stop          → Emergency stop (damp)

Usage:
  python3 g1_trigger_server.py              # port 8888
  python3 g1_trigger_server.py --port 9000  # custom port
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel
import uvicorn

# Import trigger map from g1_dance_triggers
sys.path.insert(0, str(Path(__file__).parent))
from g1_dance_triggers import TRIGGER_MAP, match_trigger, REFERENCE_DIR

DDS_IFACE = "enP8p1s0"
LOCO_BIN = Path("/home/unitree/unitree_sdk2-main/build/bin/g1_loco_client")
CLIP_PLAYER = Path("/home/unitree/g1_tools/g1_zmq_clip_player.py")
ZMQ_HOST = os.environ.get("G1_ZMQ_HOST", "127.0.0.1")
ZMQ_PORT = int(os.environ.get("G1_ZMQ_PORT", "5556"))

SONIC_LAUNCH = Path("/home/unitree/launch_sonic.sh")  # gamepad_manager + ZMQ side-channel
SONIC_PID_FILE = Path("/tmp/g1_sonic.pid")
SONIC_LOG_FILE = Path("/tmp/g1_sonic.log")
SONIC_BINARY_NAME = "g1_deploy_onnx_ref"

app = FastAPI(title="G1 Motion Trigger API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

START_TIME = time.time()
HTML_PATH = Path("/home/unitree/g1_tools/gearSonicG1.html")


class TriggerRequest(BaseModel):
    text: str


class MoveRequest(BaseModel):
    vx: float = 0.0
    vy: float = 0.0
    wz: float = 0.0


class SpeakRequest(BaseModel):
    text: str
    voice: str = "en-US-GuyNeural"


@app.get("/")
def serve_panel():
    if HTML_PATH.exists():
        return FileResponse(HTML_PATH, media_type="text/html")
    return {"message": "G1 Trigger API — see /docs", "panel": "upload gearSonicG1.html to " + str(HTML_PATH)}


def get_fsm_id() -> int:
    try:
        result = subprocess.run(
            [str(LOCO_BIN), f"--network_interface={DDS_IFACE}", "--get_fsm_id"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if "current fsm_id:" in line:
                return int(line.split(":")[-1].strip())
    except Exception:
        pass
    return -1


def load_clip_info(clip_name: str) -> dict:
    csv_path = REFERENCE_DIR / clip_name / "joint_pos.csv"
    if not csv_path.exists():
        return {"frames": 0, "duration_s": 0}
    with open(csv_path) as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        frames = sum(1 for _ in reader)
    return {"frames": frames, "duration_s": round(frames / 30.0, 1)}


@app.get("/api/list")
def list_triggers():
    triggers = []
    for keywords, clip_name, desc, intensity in TRIGGER_MAP:
        clip_path = REFERENCE_DIR / clip_name
        info = load_clip_info(clip_name) if clip_path.exists() else {"frames": 0, "duration_s": 0}
        triggers.append({
            "keywords": keywords,
            "clip_name": clip_name,
            "description": desc,
            "intensity": intensity,
            "on_disk": clip_path.exists(),
            "frames": info["frames"],
            "duration_s": info["duration_s"],
        })
    return {"triggers": triggers, "total": len(triggers)}


@app.get("/api/status")
def get_status():
    fsm = get_fsm_id()
    fsm_names = {-1: "unknown", 0: "idle", 1: "damp", 4: "standup", 501: "walk", 65535: "gear-sonic"}
    return {
        "fsm_id": fsm,
        "fsm_name": fsm_names.get(fsm, f"fsm_{fsm}"),
        "uptime_s": round(time.time() - START_TIME),
        "ready": fsm == 501,
    }


@app.post("/api/trigger")
def trigger_motion(req: TriggerRequest):
    result = match_trigger(req.text)
    if result is None:
        return JSONResponse(status_code=404, content={
            "error": f"No match for: '{req.text}'",
            "hint": "Try: dance, charleston, hip hop, kick, capoeira, cartwheel",
        })

    clip_name, desc, intensity = result
    clip_path = REFERENCE_DIR / clip_name / "joint_pos.csv"
    if not clip_path.exists():
        return JSONResponse(status_code=404, content={
            "error": f"Clip not on disk: {clip_name}",
        })

    info = load_clip_info(clip_name)

    if not CLIP_PLAYER.exists():
        return JSONResponse(status_code=500, content={
            "error": f"ZMQ clip player not installed: {CLIP_PLAYER}",
        })

    # Spawn the ZMQ streamer. GEAR-SONIC deploy binary must already be running
    # with --input-type zmq and subscribed to tcp://{ZMQ_HOST}:{ZMQ_PORT}.
    try:
        proc = subprocess.Popen(
            [
                "python3", str(CLIP_PLAYER),
                "--clip", clip_name,
                "--host", ZMQ_HOST,
                "--port", str(ZMQ_PORT),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"spawn failed: {e}"})

    return {
        "matched": desc,
        "clip_name": clip_name,
        "intensity": intensity,
        "frames": info["frames"],
        "duration_s": info["duration_s"],
        "status": "streaming",
        "pid": proc.pid,
    }


@app.post("/api/move")
def move_robot(req: MoveRequest):
    """Send a Move() command. vx=forward, vy=lateral, wz=yaw rotation."""
    fsm = get_fsm_id()
    if fsm != 501:
        return JSONResponse(status_code=400, content={
            "error": f"Not in Walk mode (FSM={fsm}). Wake the robot first.",
        })
    try:
        # Clamp values for safety
        vx = max(-1.0, min(2.5, req.vx))
        vy = max(-0.5, min(0.5, req.vy))
        wz = max(-1.5, min(1.5, req.wz))
        subprocess.run(
            ["python3", "-c", f"""
import unitree_sdk2py.core.channel as ch
from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
ch.ChannelFactoryInitialize(0, "{DDS_IFACE}")
loco = LocoClient()
loco.SetTimeout(3.0)
loco.Init()
loco.Move({vx}, {vy}, {wz}, True)
"""],
            capture_output=True, text=True, timeout=5,
        )
        return {"vx": vx, "vy": vy, "wz": wz, "status": "sent"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/speak")
def speak_text(req: SpeakRequest):
    """Text-to-speech via edge-tts → G1 speakers."""
    if not req.text.strip():
        return JSONResponse(status_code=400, content={"error": "Empty text"})
    try:
        wav_path = f"/tmp/g1_speak_{int(time.time()*1000)}.wav"
        # Generate audio with edge-tts
        tts_result = subprocess.run(
            ["python3", "-m", "edge_tts", "--text", req.text, "--voice", req.voice,
             "--write-media", wav_path],
            capture_output=True, text=True, timeout=15,
        )
        if tts_result.returncode != 0:
            return JSONResponse(status_code=500, content={
                "error": "TTS failed", "detail": tts_result.stderr[:300]
            })
        # Play through G1 speakers
        play_result = subprocess.run(
            ["python3", "/home/unitree/g1_tools/g1_play_clip.py", wav_path],
            capture_output=True, text=True, timeout=30,
        )
        # Cleanup
        try:
            os.remove(wav_path)
        except OSError:
            pass
        return {
            "text": req.text,
            "voice": req.voice,
            "status": "spoken",
            "output": play_result.stdout.strip()[-200:],
        }
    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=504, content={"error": "TTS or playback timed out"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# Arnold / Terminator preset phrases
ARNOLD_LINES = {
    "illbeback": "I'll be back.",
    "hastalavista": "Hasta la vista, baby.",
    "terminated": "You are terminated.",
    "getdown": "Get down!",
    "comewitme": "Come with me if you want to live.",
    "nowyouknow": "Now you know why I cry.",
    "whoisyourdaddy": "Who is your daddy and what does he do?",
    "gettothecopter": "Get to the chopper!",
    "its_not_a_tumor": "It's not a tumor!",
    "consider_that_a_divorce": "Consider that a divorce.",
    "chill_out": "Chill out, dickwad.",
    "talk_to_the_hand": "Talk to the hand.",
}


@app.get("/api/arnold")
def list_arnold():
    return {"lines": ARNOLD_LINES}


@app.post("/api/arnold/{line_id}")
def speak_arnold(line_id: str):
    text = ARNOLD_LINES.get(line_id)
    if not text:
        return JSONResponse(status_code=404, content={
            "error": f"Unknown line: {line_id}",
            "available": list(ARNOLD_LINES.keys()),
        })
    # Try Dell voice clone server first, fallback to edge-tts
    try:
        import urllib.request
        import json
        dell_url = "http://10.1.10.198:8889/api/speak"
        req_data = json.dumps({"text": text, "exaggeration": 0.6}).encode()
        http_req = urllib.request.Request(dell_url, data=req_data,
                                          headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(http_req, timeout=20) as resp:
            if resp.status == 200:
                wav_data = resp.read()
                wav_path = f"/tmp/g1_arnold_{int(time.time()*1000)}.wav"
                with open(wav_path, "wb") as f:
                    f.write(wav_data)
                play_result = subprocess.run(
                    ["python3", "/home/unitree/g1_tools/g1_play_clip.py", wav_path],
                    capture_output=True, text=True, timeout=30,
                )
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
                return {"text": text, "voice": "arnold-clone", "status": "spoken",
                        "output": play_result.stdout.strip()[-200:]}
    except Exception as e:
        pass  # Fallback to edge-tts
    return speak_text(SpeakRequest(text=text, voice="en-US-GuyNeural"))


CLIPS_DIR = Path("/home/unitree/g1_tools/clips")


@app.get("/api/clips")
def list_clips():
    """List original sound clips in CLIPS_DIR (excludes tour intro/outro)."""
    if not CLIPS_DIR.exists():
        return {"clips": []}
    exts = (".wav", ".mp3", ".ogg", ".flac", ".m4a")
    excluded_prefixes = ("auto_tour_",)
    clips = sorted(
        p.name for p in CLIPS_DIR.iterdir()
        if p.suffix.lower() in exts and not p.name.startswith(excluded_prefixes)
    )
    return {"clips": clips}


@app.post("/api/clip/{name}")
def play_clip(name: str):
    """Play an audio clip from CLIPS_DIR through G1 speakers."""
    # Sanitize: no path traversal, must be a real file in CLIPS_DIR.
    if "/" in name or ".." in name:
        return JSONResponse(status_code=400, content={"error": "invalid name"})
    path = CLIPS_DIR / name
    if not path.is_file():
        return JSONResponse(status_code=404, content={"error": f"not found: {name}"})
    try:
        result = subprocess.run(
            ["python3", "/home/unitree/g1_tools/g1_play_clip.py", str(path), "--gain", "2.5"],
            capture_output=True, text=True, timeout=30,
        )
        return {
            "clip": name,
            "status": "played" if result.returncode == 0 else "error",
            "output": result.stdout.strip()[-200:] or result.stderr.strip()[-200:],
        }
    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=504, content={"error": "playback timed out"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def _sonic_running_pid() -> int:
    """Return live pid if the sonic binary is running, else 0."""
    # Prefer pidfile; fall back to pgrep on the binary name.
    if SONIC_PID_FILE.exists():
        try:
            pid = int(SONIC_PID_FILE.read_text().strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            try:
                SONIC_PID_FILE.unlink()
            except OSError:
                pass
    try:
        result = subprocess.run(
            ["pgrep", "-x", SONIC_BINARY_NAME],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return 0


@app.get("/api/sonic/status")
def sonic_status():
    pid = _sonic_running_pid()
    tail = ""
    if SONIC_LOG_FILE.exists():
        try:
            with SONIC_LOG_FILE.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2048))
                tail = f.read().decode("utf-8", errors="replace")[-2048:]
        except OSError:
            pass
    return {"running": pid > 0, "pid": pid, "log_tail": tail}


# --- Unitree native gesture / training-recording actions ---

_arm_client = None

def _get_arm_client():
    """Lazy init. Requires ChannelFactoryInitialize on DDS_IFACE."""
    global _arm_client
    if _arm_client is not None:
        return _arm_client
    import unitree_sdk2py.core.channel as ch
    from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient
    try:
        ch.ChannelFactoryInitialize(0, DDS_IFACE)
    except Exception:
        pass  # already initialized elsewhere
    c = G1ArmActionClient()
    c.SetTimeout(10.0)
    c.Init()
    _arm_client = c
    return _arm_client


class ActionRequest(BaseModel):
    id: int | None = None
    name: str | None = None


@app.get("/api/actions")
def list_actions():
    """Return live firmware gesture + training-recording lists."""
    try:
        code, result = _get_arm_client().GetActionList()
        if code != 0 or not result:
            return JSONResponse(status_code=500, content={"error": f"GetActionList failed (code {code})"})
        gestures, recordings = (result[0] if len(result) > 0 else []), (result[1] if len(result) > 1 else [])
        return {"gestures": gestures, "recordings": recordings}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/action")
def run_action(req: ActionRequest):
    """Trigger an arm gesture by id OR training recording by name."""
    import json as _json
    client = _get_arm_client()
    try:
        if req.id is not None:
            # Integer id path — arm gestures.
            code, data = client._Call(7106, _json.dumps({"data": req.id}))
            return {"kind": "gesture", "id": req.id, "code": code, "msg": data}
        if req.name is not None:
            # Training-recording path is unsupported. Sending {"name": ...} to API 7106
            # silently poisons the arm SDK into ARMSDK_OCCUPIED on this firmware —
            # subsequent gesture calls return 7400 until the robot's FSM is reset.
            # The official path for recordings is the Unitree Explore app.
            return JSONResponse(status_code=400, content={
                "error": "recordings-by-name not supported — use Unitree Explore app",
                "name": req.name,
            })
        return JSONResponse(status_code=400, content={"error": "need id"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/sonic/start")
def sonic_start():
    if not SONIC_LAUNCH.exists():
        return JSONResponse(status_code=500, content={
            "error": f"launch script not found: {SONIC_LAUNCH}",
        })
    existing = _sonic_running_pid()
    if existing:
        return JSONResponse(status_code=409, content={
            "error": "sonic already running", "pid": existing,
        })
    try:
        log_fh = open(SONIC_LOG_FILE, "wb")
        proc = subprocess.Popen(
            ["bash", str(SONIC_LAUNCH)],
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        SONIC_PID_FILE.write_text(str(proc.pid))
        return {"status": "starting", "pid": proc.pid, "log": str(SONIC_LOG_FILE)}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"spawn failed: {e}"})


@app.post("/api/sonic/stop")
def sonic_stop():
    import signal
    pid = _sonic_running_pid()
    if not pid:
        return {"status": "not_running"}
    # The launch script spawns the binary; kill the whole process group.
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            subprocess.run(["pkill", "-TERM", "-x", SONIC_BINARY_NAME], timeout=3)
        except Exception:
            pass
    # Clean pidfile regardless.
    try:
        SONIC_PID_FILE.unlink()
    except OSError:
        pass
    return {"status": "stopped", "pid": pid}


@app.post("/api/stop")
def emergency_stop():
    fsm = get_fsm_id()
    try:
        subprocess.run(
            [str(LOCO_BIN), f"--network_interface={DDS_IFACE}", "--damp"],
            capture_output=True, text=True, timeout=5,
        )
        return {"status": "damped", "previous_fsm": fsm}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/wake")
def wake_robot():
    try:
        result = subprocess.run(
            ["python3", "/home/unitree/g1_tools/g1_wake.py"],
            capture_output=True, text=True, timeout=60,
        )
        fsm = get_fsm_id()
        return {"status": "awake" if fsm == 501 else "partial", "fsm_id": fsm, "output": result.stdout[-500:]}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    print(f"[SERVER] G1 Trigger API on http://{args.host}:{args.port}")
    print(f"[SERVER] Web panel: open gearSonicG1.html and set robot IP to this address")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

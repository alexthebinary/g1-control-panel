# G1 Control Panel

Web panel + server stack for controlling a Unitree G1 EDU+ running NVIDIA GEAR-SONIC (GR00T WholeBodyControl). Drive locomotion, trigger motion clips over ZMQ, and play Arnold-clone TTS through the robot's speakers.

**Live panel:** https://alexthebinary.github.io/g1-control-panel/ (set the Robot IP field to your G1 — only reachable from your LAN)

## What runs where

```
┌───────────────────────┐          ┌──────────────────────────┐
│  Browser (panel)      │──HTTP──▶│  G1 Jetson (:8888)       │
│  index.html           │          │  g1_trigger_server.py    │
└───────────────────────┘          │  g1_zmq_clip_player.py   │
          │                        │  + launch_sonic.sh       │
          │ HTTP                   │  ├─ spawns ──▶ GEAR-SONIC│
          ▼                        │  │               (tcp:5556)│
┌───────────────────────┐          │  └─ proxies ──▶ Dell     │
│  Dell (:8889)         │◀─────────┤                           │
│  g1_voice_server.py   │          └──────────────────────────┘
│  (Chatterbox TTS on   │
│   CUDA, Arnold clone) │
└───────────────────────┘
```

- **Panel** (`index.html`) — static HTML/CSS/JS, no build step. Calls the trigger server at whatever IP you type into the Robot IP field.
- **Trigger server** (`g1_trigger_server.py`) — FastAPI on G1 port 8888. Exposes `/api/wake`, `/api/stop`, `/api/move`, `/api/trigger`, `/api/sonic/{start,stop,status}`, `/api/speak`, `/api/arnold/*`. Wraps Unitree SDK (`g1_loco_client`) + spawns GEAR-SONIC + fans out voice requests to the Dell.
- **ZMQ clip player** (`g1_zmq_clip_player.py`) — reads `joint_pos.csv` + `joint_vel.csv` + `body_quat.csv` for a GEAR-SONIC reference clip and streams 29-joint pose frames to `tcp://127.0.0.1:5556` at 50 Hz using the `ZMQPackedMessageSubscriber` wire format (v1 header, little-endian).
- **Voice server** (`g1_voice_server.py`) — FastAPI on Dell port 8889. Chatterbox TTS with `arnold.wav` as the reference voice, returns 24 kHz mono WAV.
- **Launch scripts** — `launch_sonic.sh` uses `--input-type gamepad_manager` (R3 primary + ZMQ side-channel). `launch_sonic_zmq.sh` uses pure `--input-type zmq_manager` (web-only, no R3 — not recommended, hip-roll joints overheat fast in idle).

## Operator flow

1. **R3 remote** → damp → standup → walk (or hit `Wake` in the panel).
2. Panel **Sonic** button → launches `launch_sonic.sh` on G1 in gamepad_manager mode. ~15 s for TRT engines to load.
3. **R3 Start** → policy active; walk/dance with the remote.
4. Click any web clip button (Charleston, Cartwheel, Bruce Lee, …) → `g1_zmq_clip_player.py` streams the clip to port 5556.
5. **R3 F1** → robot switches from GAMEPAD to ZMQ input → plays the buffered clip.
6. **R3 D-pad** → back to GAMEPAD control.
7. `Stop GEAR-SONIC` chip to kill the binary; `STOP` chip for emergency damp.

## Prerequisites (G1 side)

- Unitree G1 EDU+ on JetPack 6.2.1
- TensorRT **10.7** (not 10.3 from apt — wrong TRT version produces garbage motor commands)
- ONNX Runtime 1.16.3
- GR00T-WholeBodyControl deployed under `/home/unitree/GR00T-WholeBodyControl/gear_sonic_deploy` with `reference/example_full/` populated
- `g1_loco_client` built from `unitree_sdk2-main`
- DDS interface **`enP8p1s0`** (not `lo`)
- Python deps on G1: `fastapi`, `uvicorn`, `pydantic`, `pyzmq`, `numpy`

## Prerequisites (Dell side)

- NVIDIA GPU with CUDA 12.8
- PyTorch 2.11.0+cu128 (not torchaudio-encoder-dependent; we write WAV via `soundfile`)
- `chatterbox-tts`, `soundfile`, `fastapi`, `uvicorn`
- An `arnold.wav` reference clip at the path configured in `g1_voice_server.py`

## Known quirks

- GEAR-SONIC reports `fsm_id: 65535` while running — the panel relabels this as "gear-sonic"; don't treat as disconnected.
- `launch_sonic_zmq.sh` (pure ZMQ) puts the robot in a high-torque idle pose while waiting for pose streams — hip-roll joints go 40 → 100 °C in 10–15 s per prior incident. Use `launch_sonic.sh` (gamepad_manager) unless you have a reason not to.
- `/api/stop` damp still fires when GEAR-SONIC is active, but the policy fights it; prefer `/api/sonic/stop` first.

## License

MIT. Not affiliated with Unitree or NVIDIA.

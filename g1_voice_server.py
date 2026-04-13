#!/usr/bin/env python3
"""
g1_voice_server.py — Arnold voice clone TTS server on Dell.

Runs Chatterbox TTS with arnold.wav as the voice reference.
G1 fetches generated audio over HTTP and plays through speakers.

Endpoints:
  POST /api/speak  {"text": "I'll be back"}  → returns WAV audio
  GET  /api/health                            → server status

Usage:
  python3 g1_voice_server.py                  # port 8889
  python3 g1_voice_server.py --port 9000
  python3 g1_voice_server.py --cpu            # force CPU mode
"""
import argparse
import io
import time

import torch
import torchaudio
import soundfile as sf
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="G1 Voice Clone Server", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ARNOLD_WAV = "/home/alex/.openclaw/workspace/workspace/media/arnold.wav"
MODEL = None
DEVICE = None
START_TIME = time.time()


class SpeakRequest(BaseModel):
    text: str
    exaggeration: float = 0.5  # 0.0 = flat, 1.0 = expressive


def load_model(device: str = "auto"):
    global MODEL, DEVICE
    if device == "auto":
        DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        DEVICE = device
    print(f"[VOICE] Loading Chatterbox on {DEVICE}...")
    from chatterbox.tts import ChatterboxTTS
    MODEL = ChatterboxTTS.from_pretrained(device=DEVICE)
    print(f"[VOICE] Model loaded on {DEVICE}")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "device": DEVICE,
        "model_loaded": MODEL is not None,
        "uptime_s": round(time.time() - START_TIME),
    }


@app.post("/api/speak")
def speak(req: SpeakRequest):
    if MODEL is None:
        return JSONResponse(status_code=503, content={"error": "Model not loaded"})
    if not req.text.strip():
        return JSONResponse(status_code=400, content={"error": "Empty text"})

    t0 = time.time()
    wav = MODEL.generate(
        req.text,
        audio_prompt_path=ARNOLD_WAV,
        exaggeration=req.exaggeration,
    )
    gen_time = time.time() - t0

    # Convert to WAV bytes (soundfile wants [frames, channels])
    audio = wav.detach().cpu().numpy()
    if audio.ndim == 2:
        audio = audio.T  # [channels, samples] -> [samples, channels]
    buf = io.BytesIO()
    sf.write(buf, audio, MODEL.sr, format="WAV", subtype="PCM_16")
    buf.seek(0)

    duration = wav.shape[-1] / MODEL.sr
    print(f"[VOICE] \"{req.text[:50]}\" → {duration:.1f}s audio in {gen_time:.1f}s (RTF={gen_time/duration:.2f})")

    return Response(
        content=buf.read(),
        media_type="audio/wav",
        headers={
            "X-Duration": f"{duration:.2f}",
            "X-Gen-Time": f"{gen_time:.2f}",
            "X-Device": DEVICE,
        },
    )


def main():
    ap = argparse.ArgumentParser(description="Arnold voice clone TTS server")
    ap.add_argument("--port", type=int, default=8889)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--cpu", action="store_true", help="Force CPU mode")
    args = ap.parse_args()

    load_model(device="cpu" if args.cpu else "auto")
    print(f"[VOICE] Server on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
g1_zmq_clip_player.py — Stream a recorded clip to GEAR-SONIC over ZMQ.

Reads joint_pos.csv / joint_vel.csv / body_quat.csv from a clip directory
and publishes it to the GEAR-SONIC deploy binary's pose topic at 50 Hz
(streamed motion mode). The WBC policy handles balance during playback.

Protocol matches test_zmq_manager.py (ZMQPackedMessageSubscriber, v1):
  - command topic: enter/exit streamed motion mode
  - pose topic:    f32 joint_pos[N,29], joint_vel[N,29], body_quat[N,4] (root, w,x,y,z),
                   i64 frame_index[N], u8 catch_up

Usage:
  python3 g1_zmq_clip_player.py <clip_dir>
  python3 g1_zmq_clip_player.py --clip macarena_001__A545 --host 127.0.0.1 --port 5556
"""
import argparse
import json
import struct
import sys
import time
from pathlib import Path

import numpy as np
import zmq

HEADER_SIZE = 1280
DT = 0.02  # 50 Hz
CHUNK_FRAMES = 50  # 1s chunk
NUM_JOINTS = 29

DEFAULT_CLIP_ROOTS = [
    Path("/home/unitree/GR00T-WholeBodyControl/gear_sonic_deploy/reference/example_full"),
    Path("/home/unitree/GR00T-WholeBodyControl/gear_sonic_deploy/reference/example"),
    Path("/home/alex/g1-prep/GR00T-WBC-full/gear_sonic_deploy/reference/example"),
    Path("/home/alex/g1-prep/GR00T-WBC-full/gear_sonic_deploy/reference/example_full"),
]


def _pack_header(header: dict) -> bytes:
    blob = json.dumps(header).encode("utf-8")
    if len(blob) > HEADER_SIZE:
        raise ValueError(f"header too large: {len(blob)} > {HEADER_SIZE}")
    return blob + b"\x00" * (HEADER_SIZE - len(blob))


def send_command(sock, start: bool, stop: bool, planner: bool) -> None:
    header = _pack_header({
        "v": 1, "endian": "le", "count": 1,
        "fields": [
            {"name": "start",   "dtype": "u8", "shape": [1]},
            {"name": "stop",    "dtype": "u8", "shape": [1]},
            {"name": "planner", "dtype": "u8", "shape": [1]},
        ],
    })
    data = struct.pack("BBB", int(start), int(stop), int(planner))
    sock.send(b"command" + header + data)


def send_pose(sock, joint_pos, joint_vel, body_quat, frame_indices, catch_up=False):
    n, num_joints = joint_pos.shape
    header = _pack_header({
        "v": 1, "endian": "le", "count": n,
        "fields": [
            {"name": "joint_pos",   "dtype": "f32", "shape": [n, num_joints]},
            {"name": "joint_vel",   "dtype": "f32", "shape": [n, num_joints]},
            {"name": "body_quat_w", "dtype": "f32", "shape": [n, 4]},
            {"name": "frame_index", "dtype": "i64", "shape": [n]},
            {"name": "catch_up",    "dtype": "u8",  "shape": [1]},
        ],
    })
    data = b"".join([
        joint_pos.astype(np.float32).tobytes(),
        joint_vel.astype(np.float32).tobytes(),
        body_quat.astype(np.float32).tobytes(),
        frame_indices.astype(np.int64).tobytes(),
        struct.pack("B", 1 if catch_up else 0),
    ])
    sock.send(b"pose" + header + data)


def load_clip(clip_dir: Path):
    jp = np.loadtxt(clip_dir / "joint_pos.csv", delimiter=",", skiprows=1, dtype=np.float32)
    jv = np.loadtxt(clip_dir / "joint_vel.csv", delimiter=",", skiprows=1, dtype=np.float32)
    bq_all = np.loadtxt(clip_dir / "body_quat.csv", delimiter=",", skiprows=1, dtype=np.float32)

    if jp.shape[1] != NUM_JOINTS:
        raise ValueError(f"expected {NUM_JOINTS} joints, got {jp.shape[1]} in {clip_dir}")
    if jp.shape[0] != jv.shape[0] or jp.shape[0] != bq_all.shape[0]:
        raise ValueError(f"frame count mismatch in {clip_dir}")

    # body_quat.csv holds 14 bodies × [w,x,y,z]; root is body 0 → first 4 cols.
    body_quat = bq_all[:, :4]
    return jp, jv, body_quat


def resolve_clip(arg: str) -> Path:
    p = Path(arg)
    if p.exists() and p.is_dir():
        return p
    for root in DEFAULT_CLIP_ROOTS:
        candidate = root / arg
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"clip not found under any root: {arg}")


def stream_clip(sock, jp, jv, bq, *, chunk=CHUNK_FRAMES, verbose=True):
    n = jp.shape[0]
    duration = n * DT

    if verbose:
        print(f"[ZMQ] streaming {n} frames ({duration:.2f}s) in chunks of {chunk}")

    # Send command to enter streamed motion mode.
    send_command(sock, start=True, stop=False, planner=False)
    time.sleep(0.1)

    # Pre-buffer 2 chunks so the consumer always has frames ahead of real time.
    sent = 0
    prebuffer = min(2 * chunk, n)
    frame_idx = np.arange(0, prebuffer, dtype=np.int64)
    send_pose(sock, jp[:prebuffer], jv[:prebuffer], bq[:prebuffer], frame_idx, catch_up=False)
    sent = prebuffer
    if verbose:
        print(f"[ZMQ] prebuffered {sent}/{n}")

    # Stream remaining in real time, one chunk per (chunk*DT) seconds.
    period = chunk * DT
    next_send = time.monotonic() + period
    while sent < n:
        now = time.monotonic()
        sleep = next_send - now
        if sleep > 0:
            time.sleep(sleep)
        end = min(sent + chunk, n)
        frame_idx = np.arange(sent, end, dtype=np.int64)
        send_pose(sock, jp[sent:end], jv[sent:end], bq[sent:end], frame_idx, catch_up=False)
        if verbose:
            print(f"[ZMQ] sent {end}/{n}")
        sent = end
        next_send += period

    # Wait for last chunk to finish playing before stopping.
    time.sleep(chunk * DT + 0.25)

    # Leave motion mode running — caller decides when to stop. For standalone
    # invocation main() sends the stop.


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", nargs="?", help="Clip dir path or name under default reference/")
    ap.add_argument("--clip", dest="clip_kw", help="Same as positional, for flag style")
    ap.add_argument("--host", default="127.0.0.1", help="Publisher bind host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=5556)
    ap.add_argument("--chunk", type=int, default=CHUNK_FRAMES)
    ap.add_argument("--no-stop", action="store_true", help="Leave motion mode active after clip")
    args = ap.parse_args()

    clip_arg = args.clip or args.clip_kw
    if not clip_arg:
        ap.error("clip path or name required")

    clip_dir = resolve_clip(clip_arg)
    jp, jv, bq = load_clip(clip_dir)
    print(f"[ZMQ] loaded {clip_dir.name}: {jp.shape[0]} frames × {jp.shape[1]} joints")

    ctx = zmq.Context()
    sock = ctx.socket(zmq.PUB)
    endpoint = f"tcp://{args.host if args.host != '127.0.0.1' else '*'}:{args.port}"
    sock.bind(endpoint)
    print(f"[ZMQ] bound {endpoint}, waiting 0.5s for subscriber…")
    time.sleep(0.5)

    try:
        stream_clip(sock, jp, jv, bq, chunk=args.chunk)
        if not args.no_stop:
            print("[ZMQ] sending stop")
            send_command(sock, start=False, stop=True, planner=False)
            time.sleep(0.3)
    finally:
        sock.close()
        ctx.term()


if __name__ == "__main__":
    main()

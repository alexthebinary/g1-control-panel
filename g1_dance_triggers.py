#!/usr/bin/env python3
"""
g1_dance_triggers.py — Voice/text triggers → GEAR-SONIC motion clips.

Maps natural language triggers to deployed motion clips on G1.
Works with the C++ GEAR-SONIC deploy binary or standalone via joint replay.

Deployed clips (from gear_sonic_deploy/reference/example/):
  - dance_in_da_party_001__A464   (party dance)
  - macarena_001__A545            (macarena)
  - forward_lunge_R_001__A359_M   (lunge)
  - neutral_kick_R_001__A543      (kick)
  - squat_001__A359               (squat)
  - tired_forward_lunge_R_001__A359_M (tired lunge)
  - tired_one_leg_jumping_R_001__A359 (one-leg jump)
  - walking_quip_360_R_002__A428  (360 walk)

Usage:
  python3 g1_dance_triggers.py --list
  python3 g1_dance_triggers.py --trigger "dance"
  python3 g1_dance_triggers.py --trigger "do the macarena"
  python3 g1_dance_triggers.py --trigger "kick"
"""
import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Optional

SONIC_DEPLOY = Path("/home/unitree/GR00T-WholeBodyControl/gear_sonic_deploy")
REFERENCE_DIR = SONIC_DEPLOY / "reference" / "example"
REFERENCE_FULL_DIR = SONIC_DEPLOY / "reference" / "example_full"

# Trigger → clip mapping. Keywords are matched case-insensitive.
# Each entry: (keywords, clip_name, description, intensity)
TRIGGER_MAP = [
    # === GEAR-SONIC originals (8) ===
    (["dance", "party", "groove", "rizz", "vibe"],
     "dance_in_da_party_001__A464", "Party dance", "high"),
    (["macarena", "classic dance", "retro"],
     "macarena_001__A545", "Macarena dance", "high"),
    (["kick", "martial", "karate"],
     "neutral_kick_R_001__A543", "Standing kick", "medium"),
    (["lunge", "stretch", "exercise"],
     "forward_lunge_R_001__A359_M", "Forward lunge", "medium"),
    (["squat", "crouch", "sit"],
     "squat_001__A359", "Deep squat", "medium"),
    (["spin", "360", "turn around", "twirl"],
     "walking_quip_360_R_002__A428", "360 walk-spin", "medium"),
    (["tired", "exhausted", "lazy", "sleepy"],
     "tired_forward_lunge_R_001__A359_M", "Tired lunge", "low"),

    # === NEW: openhe/g1-retargeted-motions (16) ===
    # Dance styles (BEFORE "jump/hop" so "hip hop" matches here first)
    (["hip hop", "hiphop", "r&b", "rnb", "street"],
     "AnnaCortesi_RandB_C3D", "R&B / Hip-hop dance", "high"),
    (["charleston", "swing", "20s", "twenties"],
     "Charleston_dance", "Charleston dance", "high"),

    # Jump (after hip-hop so "hip hop" doesn't match "hop")
    (["jump", "hop", "bounce"],
     "tired_one_leg_jumping_R_001__A359", "One-leg jump", "high"),
    (["capoeira", "brazilian", "acrobatic"],
     "Capoeira_Theodoros_v2_C3D", "Capoeira", "high"),
    (["happy", "joy", "celebrate", "cheer"],
     "Andria_Happy_v1_C3D", "Happy expression", "medium"),
    (["excited", "hyped", "pumped", "energy"],
     "Andria_Excited_v1_C3D", "Excited expression", "medium"),
    (["freestyle", "long dance", "performance"],
     "dance1_subject1", "Freestyle dance 1", "high"),
    (["dance battle", "showoff", "show off"],
     "dance2_subject1", "Freestyle dance 2", "high"),

    # Martial arts
    (["bruce lee", "kung fu", "martial arts pose"],
     "Bruce_Lee_pose", "Bruce Lee pose", "medium"),
    (["roundhouse", "spinning kick"],
     "Roundhouse_kick", "Roundhouse kick", "high"),
    (["side kick", "front kick"],
     "Side_kick", "Side kick", "medium"),
    (["fight", "combat", "punch"],
     "fight1_subject2", "Fight sequence", "high"),

    # Locomotion & utility
    (["crawl", "army crawl", "stealth"],
     "A11-_Crawl_stageii", "Military crawl", "medium"),
    (["sway", "idle", "chill", "relax"],
     "A2-_Sway_stageii", "Gentle sway", "low"),
    (["cartwheel", "flip", "acrobat"],
     "D6-_CartWheel_stageii", "Cartwheel", "high"),
    (["gesture", "talk", "conversation", "present"],
     "D3_-_Conversation_Gestures_stageii", "Conversation gestures", "low"),
    (["fall", "get up", "recover", "stumble"],
     "fallAndGetUp1_subject1", "Fall and get up", "high"),
]


def match_trigger(text: str) -> Optional[tuple]:
    """Match input text to a motion clip. Returns (clip_name, description, intensity) or None."""
    text_lower = text.lower()
    for keywords, clip_name, desc, intensity in TRIGGER_MAP:
        for kw in keywords:
            if kw in text_lower:
                return clip_name, desc, intensity
    return None


def list_clips():
    """List all available clips with trigger keywords."""
    print(f"\n{'Trigger Keywords':<40} {'Clip':<45} {'Intensity'}")
    print("-" * 95)
    for keywords, clip_name, desc, intensity in TRIGGER_MAP:
        kw_str = ", ".join(keywords)
        print(f"{kw_str:<40} {desc:<45} {intensity}")

    # Check which clips actually exist on disk
    print(f"\n--- On-disk status ({REFERENCE_DIR}) ---")
    for _, clip_name, desc, _ in TRIGGER_MAP:
        clip_path = REFERENCE_DIR / clip_name
        exists = clip_path.exists()
        status = "OK" if exists else "MISSING"
        print(f"  [{status}] {clip_name}")


def load_joint_trajectory(clip_name: str) -> list:
    """Load joint_pos.csv from a clip directory. Returns list of joint angle rows."""
    csv_path = REFERENCE_DIR / clip_name / "joint_pos.csv"
    if not csv_path.exists():
        # Try full reference
        csv_path = REFERENCE_FULL_DIR / clip_name / "joint_pos.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"No joint_pos.csv for clip: {clip_name}")

    rows = []
    with open(csv_path) as f:
        reader = csv.reader(f)
        header = next(reader)  # skip header
        for row in reader:
            rows.append([float(v) for v in row])
    return rows


def replay_clip(clip_name: str, speed: float = 1.0, dry_run: bool = False):
    """Replay a motion clip by sending joint positions via LocoClient or low-level SDK.

    For now, this prints the trajectory info. Full replay requires the GEAR-SONIC
    C++ binary or low-level motor commands.
    """
    try:
        trajectory = load_joint_trajectory(clip_name)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return False

    n_frames = len(trajectory)
    n_joints = len(trajectory[0]) if trajectory else 0
    duration_s = n_frames / 30.0 / speed  # 30fps reference

    print(f"[CLIP] {clip_name}")
    print(f"       {n_frames} frames, {n_joints} joints, {duration_s:.1f}s @ {speed}x speed")

    if dry_run:
        print("       [DRY RUN] Would replay via GEAR-SONIC deploy binary")
        return True

    # TODO: Integrate with GEAR-SONIC C++ binary for actual replay
    # The deploy binary reads from reference/ directories and executes
    # via the WBC policy. For now, print trajectory stats.
    print(f"       [READY] Trajectory loaded — needs GEAR-SONIC binary integration")
    print(f"       Run: cd {SONIC_DEPLOY} && ./build/bin/g1_deploy --motion {clip_name}")
    return True


def trigger(text: str, dry_run: bool = False):
    """Process a voice/text trigger and play the matching clip."""
    result = match_trigger(text)
    if result is None:
        print(f"[TRIGGER] No match for: '{text}'")
        print(f"          Try: dance, macarena, kick, squat, jump, spin, tired")
        return False

    clip_name, desc, intensity = result
    print(f"[TRIGGER] '{text}' → {desc} ({intensity} intensity)")
    return replay_clip(clip_name, dry_run=dry_run)


def main():
    ap = argparse.ArgumentParser(description="Voice/text → GEAR-SONIC motion clips")
    ap.add_argument("--list", action="store_true", help="List all available clips")
    ap.add_argument("--trigger", type=str, help="Trigger text (e.g. 'dance', 'do the macarena')")
    ap.add_argument("--dry-run", action="store_true", help="Don't actually play, just show info")
    args = ap.parse_args()

    if args.list:
        list_clips()
    elif args.trigger:
        trigger(args.trigger, dry_run=args.dry_run)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

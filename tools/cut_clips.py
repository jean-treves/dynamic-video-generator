#!/usr/bin/env python3
"""
Cut the segments listed in cutlist.csv into LoRA training clips.

Each cutlist.csv row (film,start,duration_s,character) becomes
`clips/<Character>_NN.mp4`: re-encoded to 24 fps H.264, no audio, ready for the
LTX trainer's preprocessing. Character names are validated against
characters.json. Pre-existing clips (<Character>.mp4, no suffix) are never
touched.

Usage:
    python3 cut_clips.py                 # reads cutlist.csv next to the script
    python3 cut_clips.py another_list.csv
"""
from __future__ import annotations

import csv
import json
import logging
import re
import subprocess
import sys
import os
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent                       # repository root
FILMS_DIR = Path(os.environ.get("PHOS_FILMS_DIR", "./films"))
CLIPS_DIR = ROOT / "clips"
ROSTER_PATH = ROOT / "characters.json"

DUR_MIN_S, DUR_MAX_S = 1.5, 5.0
TARGET_FPS = 24

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("cut_clips")


def _parse_time(raw: str) -> float:
    """'75.5' | '1:15.5' | '0:01:15' -> secondes."""
    parts = raw.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"timestamp illisible : {raw!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def _next_index(character: str) -> int:
    """Next free index for <character>_NN.mp4 (existing files are kept)."""
    pat = re.compile(rf"^{re.escape(character)}_(\d+)\.mp4$")
    taken = [int(m.group(1)) for f in CLIPS_DIR.glob(f"{character}_*.mp4")
             if (m := pat.match(f.name))]
    return max(taken, default=0) + 1


def _cut(film: Path, start_s: float, dur_s: float, out: Path) -> None:
    """ffmpeg: -ss before -i (fast seek, accurate when re-encoding)."""
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{start_s:.3f}", "-i", str(film),
         "-t", f"{dur_s:.3f}", "-an", "-r", str(TARGET_FPS),
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
        capture_output=True, timeout=180, check=True)


def _probe(out: Path) -> tuple[float, str]:
    """(duration s, reported fps) of the produced clip — post-cut check."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate:format=duration",
         "-of", "default=nw=1", str(out)],
        capture_output=True, text=True, timeout=30, check=True)
    dur = fps = "?"
    for line in res.stdout.splitlines():
        if line.startswith("duration="):
            dur = line.split("=", 1)[1]
        elif line.startswith("avg_frame_rate="):
            fps = line.split("=", 1)[1]
    return float(dur), fps


def main() -> None:
    cutlist = Path(sys.argv[1]) if len(sys.argv) > 1 else TOOLS_DIR / "cutlist.csv"
    if not cutlist.is_file():
        logger.error("cutlist introuvable : %s", cutlist)
        sys.exit(1)
    roster = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    valid_ids = {c["id"] for c in roster["characters"]}

    rows = [r for r in csv.DictReader(
        line for line in cutlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#"))]
    if not rows:
        logger.error("cutlist empty — fill in film,start,duration_s,character "
                     "(planches : dataset_tools/sheets/index.html)")
        sys.exit(1)

    made: list[Path] = []
    errors = 0
    for i, row in enumerate(rows, 1):
        try:
            film = FILMS_DIR / row["film"].strip()
            if not film.is_file():
                raise FileNotFoundError(f"film absent : {film.name}")
            character = row["character"].strip()
            if character not in valid_ids:
                raise ValueError(f"character {character!r} not in {sorted(valid_ids)}")
            start_s = _parse_time(row["start"])
            dur_s = float(row["duration_s"])
            if not DUR_MIN_S <= dur_s <= DUR_MAX_S:
                raise ValueError(f"duration {dur_s}s outside [{DUR_MIN_S};{DUR_MAX_S}]")
            out = CLIPS_DIR / f"{character}_{_next_index(character):02d}.mp4"
            _cut(film, start_s, dur_s, out)
            real_dur, fps = _probe(out)
            logger.info("ligne %d : %s [%s +%.1fs] -> %s (%.2fs @ %s)",
                        i, film.name, row["start"].strip(), dur_s,
                        out.name, real_dur, fps)
            made.append(out)
        except (KeyError, ValueError, FileNotFoundError,
                subprocess.SubprocessError) as exc:
            errors += 1
            logger.error("row %d skipped: %s", i, exc)

    # Summary: per-character breakdown plus missing captions.
    logger.info("=== %d clip(s) created, %d error(s) ===", len(made), errors)
    for pid in sorted(valid_ids):
        n = len(list(CLIPS_DIR.glob(f"{pid}.mp4"))) + \
            len(list(CLIPS_DIR.glob(f"{pid}_*.mp4")))
        flag = "  ⚠️ < 2 clips" if n < 2 else ""
        logger.info("  %-15s %d clip(s)%s", pid, n, flag)
    missing = [c.name for c in sorted(CLIPS_DIR.glob("*.mp4"))
               if not c.with_suffix(".txt").is_file()]
    if missing:
        logger.warning("missing captions (%d): %s — generate them before the "
                       "Kaggle upload", len(missing), ", ".join(missing))


if __name__ == "__main__":
    main()

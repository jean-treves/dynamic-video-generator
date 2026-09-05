#!/usr/bin/env python3
"""
Contact sheets of the source clips, to pick LoRA dataset segments.

For each film in FILMS_DIR: ffmpeg scene detection (`select=gt(scene,T)`),
one thumbnail extracted per scene, then one HTML page per film (a grid of
timestamped thumbnails) plus an index.html. You browse the pages and copy the
chosen timestamps into cutlist.csv.

Usage:
    python3 make_contact_sheets.py            # every film
    python3 make_contact_sheets.py gine.mp4   # a single film (exact name)
"""
from __future__ import annotations

import html
import logging
import re
import subprocess
import sys
import os
from pathlib import Path

FILMS_DIR = Path(os.environ.get("PHOS_FILMS_DIR", "./films"))
OUT_DIR = Path(__file__).resolve().parent / "sheets"
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v", ".mkv")

SCENE_THRESHOLD = 0.25   # cut-detector sensitivity (0-1)
MAX_SCENES = 96          # beyond that: uniform downsampling
MIN_SCENES = 12          # below that: periodic fallback
FALLBACK_STEP_S = 8.0    # step of the periodic fallback
THUMB_W = 320            # thumbnail width (px)

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("contact_sheets")


def _duration_s(video: Path) -> float:
    """Video file duration in seconds (0.0 if ffprobe fails)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(video)],
            capture_output=True, text=True, timeout=30, check=True)
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return 0.0


def _scene_times(video: Path) -> list[float]:
    """
    Timestamps (s) of the scene changes detected by ffmpeg.

    Decodes the whole film (the slowest step here: ~1-2 min per 720p film).
    Tops up with periodic sampling when too few scenes are found, and
    downsamples uniformly when there are too many.
    """
    cmd = ["ffmpeg", "-hide_banner", "-i", str(video),
           "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
           "-f", "null", "-"]
    # errors="replace": ffmpeg output can contain non-UTF-8 bytes (macOS NFD
    # accented file names) that would otherwise crash text=True.
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    times = sorted({round(float(m), 1)
                    for m in re.findall(r"pts_time:([0-9]+\.?[0-9]*)", proc.stderr)})
    dur = _duration_s(video)
    if len(times) < MIN_SCENES and dur > 0:
        periodic = [round(t, 1) for t in _frange(1.0, dur - 1.0, FALLBACK_STEP_S)]
        times = sorted(set(times) | set(periodic))
        logger.info("  scenes < %d -> periodic fallback (%d points)",
                    MIN_SCENES, len(times))
    if len(times) > MAX_SCENES:
        step = len(times) / MAX_SCENES
        times = [times[int(i * step)] for i in range(MAX_SCENES)]
        logger.info("  scenes capped at %d (downsampled)", MAX_SCENES)
    if not times:
        times = [1.0]
    return times


def _frange(start: float, stop: float, step: float) -> list[float]:
    """range() for floats, stop bound included when hit exactly."""
    vals, t = [], start
    while t <= stop:
        vals.append(t)
        t += step
    return vals


def _extract_thumb(video: Path, ts: float, out_jpg: Path) -> bool:
    """Extract one frame at ts (fast seek) into out_jpg. True on success."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{ts:.1f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2", "-q:v", "4",
             str(out_jpg)],
            capture_output=True, timeout=30, check=True)
        return out_jpg.is_file()
    except (subprocess.SubprocessError, OSError):
        return False


def _sheet_html(film: Path, entries: list[tuple[float, Path]]) -> str:
    """HTML page: grid of timestamped thumbnails for one film."""
    cells = []
    for ts, jpg in entries:
        m, s = divmod(ts, 60.0)
        label = f"{int(m):02d}:{s:04.1f} · t={ts:.1f}s"
        cells.append(
            f'<figure><img src="{html.escape(jpg.name)}" loading="lazy">'
            f"<figcaption>{label}</figcaption></figure>")
    return (
        "<!doctype html><meta charset='utf-8'>"
        f"<title>{html.escape(film.name)}</title>"
        "<style>body{background:#111;color:#ddd;font:13px -apple-system,sans-serif;"
        "margin:16px} h1{font-size:16px} .grid{display:grid;"
        f"grid-template-columns:repeat(auto-fill,minmax({THUMB_W}px,1fr));gap:10px}}"
        "figure{margin:0} img{width:100%;border-radius:4px}"
        "figcaption{color:#8ab4f8;padding:2px 0}</style>"
        f"<h1>{html.escape(film.name)} — {len(entries)} plans "
        "(copy film,start,duration_s,character into cutlist.csv)</h1>"
        f"<div class='grid'>{''.join(cells)}</div>")


def build_sheet(film: Path) -> tuple[Path, int]:
    """Build one film's sheet; returns (html page, thumbnail count)."""
    logger.info("%s (%.0f s) — detecting scenes…", film.name, _duration_s(film))
    times = _scene_times(film)
    film_dir = OUT_DIR / film.stem
    film_dir.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[float, Path]] = []
    for ts in times:
        jpg = film_dir / f"t{ts:08.1f}.jpg"
        if jpg.is_file() or _extract_thumb(film, ts, jpg):
            entries.append((ts, jpg))
    page = film_dir / "index.html"
    page.write_text(_sheet_html(film, entries), encoding="utf-8")
    logger.info("  -> %s (%d vignettes)", page, len(entries))
    return page, len(entries)


def main() -> None:
    only = set(sys.argv[1:])
    films = sorted(p for p in FILMS_DIR.iterdir()
                   if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith("."))
    if only:
        films = [f for f in films if f.name in only]
    if not films:
        logger.error("no film found in %s (filter=%s)", FILMS_DIR, only or "-")
        sys.exit(1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    links = []
    for film in films:
        page, n = build_sheet(film)
        links.append(
            f'<li><a href="{html.escape(page.parent.name)}/index.html">'
            f"{html.escape(film.name)}</a> — {n} plans, "
            f"{_duration_s(film):.0f} s</li>")
    index = OUT_DIR / "index.html"
    index.write_text(
        "<!doctype html><meta charset='utf-8'><title>Planches-contact</title>"
        "<style>body{background:#111;color:#ddd;font:15px -apple-system,sans-serif;"
        "margin:24px} a{color:#8ab4f8}</style>"
        "<h1>Planches-contact</h1><ul>" + "".join(links) + "</ul>",
        encoding="utf-8")
    logger.info("Index -> %s", index)


if __name__ == "__main__":
    main()

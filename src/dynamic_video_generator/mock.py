"""Replay mode: run the interface with no render machine and no LLM.

The video backend (LTX on a second Mac) and the local LLM are not always
there: render machine powered off, fresh clone of the repository, demo on a
laptop. In mock mode the proxy serves recorded responses that are plausible
and deterministic, so the full interface stays navigable and testable.

Enabled by ``--mock`` or ``PHOS_MOCK=1``. Every response is tagged
``"mock": true`` so no output can be mistaken for a real render.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

#: Simulated latency (seconds): enough to see the UI's loading states.
LATENCY_LLM = 0.4
LATENCY_UPSTREAM = 0.2

_REWRITES: dict[str, str] = {
    "LTX": (
        "A slow dolly-in shot of {subject}, the camera easing forward at eye level. "
        "The subject turns their head, gaze lifting, breath visible in the cold air. "
        "Background: a wide rain-darkened plaza, wet stone reflecting scattered light, "
        "fine drizzle drifting through the frame. Dramatic low-key lighting with a soft "
        "rim from a single sodium lamp, 35mm cinematic film grain, hyper-detailed, "
        "photorealistic, accompanied by distant traffic hum and the patter of rain."
    ),
    "LTX-i2v": (
        "The subject breathes slowly and turns their head a few degrees to the left, "
        "eyes tracking something off-frame. A gentle push-in over four seconds. Loose "
        "fabric stirs in a light wind, dust motes drift through the beam of light, and "
        "the illumination warms almost imperceptibly toward the end of the shot."
    ),
    "GROUND": (
        "A medium shot of {subject} standing still in an empty room, lit by one window. "
        "The camera holds steady. One slow gesture: a hand lifted, then lowered. Natural "
        "daylight, muted palette, plain background, physically plausible motion throughout."
    ),
    "AMPLIFY": (
        "An explosive low-angle shot of {subject} as shockwaves ripple outward, debris "
        "hurled through shafts of light, camera whip-panning to follow. Layered orchestral "
        "score building to a brass swell, sub-bass boom on impact, metallic whooshes, "
        "roaring ambience and a final concussive hit that shakes the frame."
    ),
    "TRANSLATE": "A cinematic wide shot at golden hour, the subject walking slowly toward camera.",
    "DIALOGUE": (
        "{original} A character says in French: \"On y est presque.\" "
        "A voice-over answers in French: \"Alors ne t'arrete pas.\""  # i18n-exempt: sample dialogue
    ),
    "IMGPROMPT": (
        "cinematic portrait of {subject}, dramatic side lighting, deep shadows, "
        "shallow depth of field, 85mm lens, muted teal and amber palette, photoreal, high detail"
    ),
    "DIRECTOR": (
        "A locked-off medium shot of {subject}. One continuous action: they step forward "
        "and stop. Natural light from frame left, soft falloff, no camera movement. "
        "Physically plausible, single subject, 35mm photoreal."
    ),
}

_DEFAULT_REWRITE = (
    "A cinematic shot of {subject}, slow push-in, volumetric light, photoreal, "
    "35mm film grain, one motion beat every two seconds."
)


def _subject(user_prompt: str) -> str:
    """Extract a short subject from the user prompt to anchor the response."""
    words = user_prompt.strip().split()
    return " ".join(words[:8]) if words else "a lone figure"


def generate(tag: str, user_prompt: str, model: str) -> tuple[str, dict[str, Any]]:
    """Return a recorded rewrite instead of calling the local LLM.

    Parameters
    ----------
    tag : str
        Tag of the requested transform (``"LTX"``, ``"GROUND"``...).
    user_prompt : str
        Prompt posted by the user, echoed back so the response visibly
        matches it.
    model : str
        Model name reported in the metadata.

    Returns
    -------
    tuple of (str, dict)
        Rewritten text and timing metadata, same shape as ``_ollama_generate``.
    """
    time.sleep(LATENCY_LLM)
    template = _REWRITES.get(tag, _DEFAULT_REWRITE)
    text = template.format(subject=_subject(user_prompt), original=user_prompt.strip())
    logger.info("MOCK %s -> %d chars (no LLM call)", tag, len(text))
    return text, {
        "duration_sec": LATENCY_LLM,
        "model": model,
        "mock": True,
        "eval_count": len(text.split()),
    }


def storyboard(n_panels: int) -> list[dict[str, str]]:
    """Return a recorded storyboard of ``n_panels`` panels."""
    time.sleep(LATENCY_LLM)
    beats = [
        ("A wide establishing shot of an empty coastal road at dawn, slow drift forward.",
         "Le jour se leve sur une route vide."),  # i18n-exempt: sample subtitle
        ("A medium shot of a figure walking into frame, camera tracking alongside.",
         "Une silhouette avance, sans se retourner."),  # i18n-exempt: sample subtitle
        ("A close-up of hands gripping a worn strap, shallow focus, morning light.",
         "Elle serre la sangle un peu plus fort."),
        ("A low-angle shot as clouds break and light floods the road, camera tilting up.",
         "La lumiere finit par passer."),
    ]
    return [
        {"prompt": beats[i % len(beats)][0], "voiceover": beats[i % len(beats)][1]}
        for i in range(max(1, n_panels))
    ]


def caption(source: str) -> str:
    """Return a recorded training caption for ``source``."""
    time.sleep(LATENCY_LLM)
    return (
        "A figure in a dark weathered coat stands at the centre of a wide concrete "
        "plaza, shoulders squared, head turning slowly to the right across the three "
        "frames. Overcast daylight from above, muted grey and umber palette, wet ground "
        "reflecting a faint sheen. The camera drifts forward slightly as loose fabric "
        "shifts in the wind."
    )


#: Recorded outputs, with the generation parameters a real sidecar carries.
#: The dashboard derives every figure it shows from these — duration, budget,
#: seconds per step — so the mock has to be dimensionally honest: the widths
#: are multiples of 32, the frame counts satisfy `frames % 8 == 1`, and the
#: render times follow the measured curve (~12 s/step inside the 16 GB budget,
#: collapsing to ~104 s/step once it swaps).
_SAMPLE_OUTPUTS = [
    # name, kind, prompt, width, height, frames, quality, steps, render_seconds
    ("lighthouse-storm", "video", "a lighthouse keeper facing a storm, slow push-in",
     704, 416, 97, "standard", 10, 121),
    ("plaza-dawn", "video", "a wide concrete plaza at dawn, drifting camera",
     704, 416, 121, "standard", 10, 152),
    ("neon-alley", "video", "a rain-slicked alley under neon signs, static shot",
     576, 320, 97, "quick", 8, 63),
    ("empty-hotel", "video", "an empty hotel lobby, orbiting camera move",
     1024, 576, 169, "high", 14, 1_452),
    ("diner-3am", "video", "a shuttered diner at 3am, gentle handheld drift",
     704, 416, 73, "quick", 8, 71),
    ("misty-coast", "video", "a coastal road swallowed by fog, locked-off shot",
     704, 416, 97, "standard", 10, 118),
    ("dusty-workshop", "video", "dust motes in a workshop beam, slow tilt up",
     576, 320, 121, "quick", 8, 74),
    ("harbour-quay", "video", "a harbour quay at night, crane shot rising",
     896, 512, 121, "high", 12, 402),
    ("portrait-01", "image", "cinematic portrait, dramatic side lighting, 85mm",
     1024, 1024, 1, "high", 4, 9),
    ("portrait-02", "image", "weathered face, hard rim light, muted palette",
     1024, 1024, 1, "high", 4, 8),
    ("backdrop-01", "image", "concept art of a fog-bound lighthouse, matte painting",
     1024, 1024, 1, "standard", 4, 7),
    ("backdrop-02", "image", "an abandoned plaza, overcast, wide establishing frame",
     1024, 1024, 1, "standard", 4, 8),
]

#: Frames per second the render backend writes.
FPS = 24


def _params(row: tuple) -> dict[str, Any]:
    """Generation parameters, in the shape a real sidecar file uses."""
    name, kind, prompt, w, h, frames, quality, steps, secs = row
    return {
        "prompt": prompt,
        "width": w,
        "height": h,
        "frames": frames,
        "fps": FPS,
        "quality": quality,
        "steps": steps,
        "render_seconds": secs,
        "seed": abs(hash(name)) % 100_000,
        "mock": True,
    }


def sidecar(path: str) -> dict[str, Any]:
    """Return the recorded parameters for ``path``, or an empty sidecar."""
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    for row in _SAMPLE_OUTPUTS:
        if row[0] == stem:
            return {"params": _params(row), "mock": True}
    return {"params": {}, "mock": True}


def poster(path: str) -> bytes:
    """Return an SVG stand-in for the thumbnail of ``path``.

    With no render backend there is no video to grab a frame from, so ffmpeg
    has nothing to work with and every tile in the gallery, the playlists and
    the dashboard collapses to an empty box. This draws a legible placeholder
    instead: a hue derived from the name (so a clip keeps the same card colour
    everywhere), the render parameters that tile actually stands for, and a
    MOCK stamp — never a picture pretending to be a real render.

    Parameters
    ----------
    path : str
        The ``?path=`` value the client asked a thumbnail for.

    Returns
    -------
    bytes
        A UTF-8 encoded SVG document, 480x270.
    """
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    row = next((r for r in _SAMPLE_OUTPUTS if r[0] == stem), None)
    hue = int(hashlib.md5(stem.encode()).hexdigest()[:4], 16) % 360
    if row:
        _, kind, _, w, h, frames, _q, _s, _sec = row
        spec = f"{w}x{h}" + (f" · {frames}f" if kind == "video" else "")
    else:
        kind, spec = "video", "no sidecar"
    glyph = "▶" if kind == "video" else "🖼"
    label = stem[:26]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 270" width="480" height="270">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="hsl({hue},48%,26%)"/><stop offset="1" stop-color="hsl({(hue + 42) % 360},52%,14%)"/>
</linearGradient></defs>
<rect width="480" height="270" fill="url(#g)"/>
<circle cx="240" cy="126" r="34" fill="none" stroke="hsl({hue},70%,82%)" stroke-opacity=".55" stroke-width="2"/>
<text x="240" y="139" font-size="26" text-anchor="middle" fill="hsl({hue},70%,88%)" fill-opacity=".8">{glyph}</text>
<rect x="0" y="214" width="480" height="56" fill="#000" fill-opacity=".34"/>
<text x="18" y="240" font-family="ui-monospace,Menlo,monospace" font-size="16" fill="#fff" fill-opacity=".92">{label}</text>
<text x="18" y="258" font-family="ui-monospace,Menlo,monospace" font-size="12" fill="#fff" fill-opacity=".6">{spec}</text>
<text x="462" y="30" font-family="ui-monospace,Menlo,monospace" font-size="12" text-anchor="end" fill="#fff" fill-opacity=".55">MOCK</text>
</svg>'''.encode()


def outputs(limit: int = 500) -> dict[str, Any]:
    """Return a recorded list of outputs to populate the interface.

    Timestamps step back one day per item so that the "last 7 days" filters
    and the timeline have something to work with.

    Parameters
    ----------
    limit : int
        Maximum number of items returned.

    Returns
    -------
    dict
        ``{"outputs": [...], "mock": true}``, same shape as the real backend.
    """
    real = demo_media(limit)
    if real:
        return {"outputs": real, "mock": True}
    now = time.time()
    items = []
    for i, row in enumerate(_SAMPLE_OUTPUTS[:limit]):
        name, kind, prompt, w, h, frames, _q, _s, _sec = row
        ext = "mp4" if kind == "video" else "png"
        # A plausible size: video clips land around 0.9 Mbit/s at these sizes.
        seconds = frames / FPS
        size = int(seconds * 115_000) if kind == "video" else int(w * h * 1.4)
        items.append({
            "name": f"{name}.{ext}",
            "path": f"/outputs/{name}.{ext}",
            "url": f"/outputs/{name}.{ext}",
            "kind": kind,
            "prompt": prompt,
            "has_sidecar": True,
            "size": size,
            "mtime_ts": now - i * 86_400,
            "params": _params(row),
            "mock": True,
        })
    return {"outputs": items, "mock": True}


#: Real output of this pipeline, committed so a clone has something honest to
#: look at. Each clip sits beside the sidecar the backend wrote for it; the
#: drawn placeholders below are only the fallback when this is empty.
DEMO_DIR = Path(__file__).resolve().parents[2] / "docs" / "demo"

_MEDIA_EXTS = {".mp4": "video", ".mov": "video", ".webm": "video",
               ".png": "image", ".jpg": "image", ".jpeg": "image"}


def demo_media(limit: int = 500) -> list[dict[str, Any]]:
    """Return the committed demo renders, newest first.

    A file is only listed when its sidecar is present: without the render
    parameters the dashboard would have nothing measured to show, and
    inventing them is exactly what this project refuses to do.

    Parameters
    ----------
    limit : int
        Maximum number of entries to return.

    Returns
    -------
    list of dict
        Output entries in the shape the client expects, or an empty list.
    """
    try:
        entries = sorted(DEMO_DIR.iterdir())
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    for path in entries:
        kind = _MEDIA_EXTS.get(path.suffix.lower())
        if not kind or not path.is_file():
            continue
        sidecar = path.with_suffix(".json")
        if not sidecar.is_file():
            logger.info("demo media without a sidecar, skipped: %s", path.name)
            continue
        try:
            params = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("demo sidecar unreadable (%s): %s", path.name, exc)
            continue
        stat = path.stat()
        items.append({
            "name": path.name,
            "path": f"/demo/{path.name}",
            "url": f"/demo/{path.name}",
            "kind": kind,
            "prompt": params.get("prompt", ""),
            "has_sidecar": True,
            "size": stat.st_size,
            "mtime_ts": stat.st_mtime,
            "params": params,
            "mock": False,
        })
        if len(items) >= limit:
            break
    items.sort(key=lambda i: i["mtime_ts"], reverse=True)
    return items


def images(limit: int = 200) -> dict[str, Any]:
    """Return the recorded image outputs for the Images tab.

    The live route walks the render backend's directories on the host. In
    replay mode there is no render backend, and walking the host is how the
    tab ended up serving a personal photo library, so this answers from the
    recording and never touches the filesystem.

    Parameters
    ----------
    limit : int
        Maximum number of entries to return.

    Returns
    -------
    dict
        ``{"images": [...], "extended": [...], "total_found": n, "mock": true}``
        — the same shape the live route returns.
    """
    real = [m for m in demo_media(limit) if m["kind"] == "image"]
    if real:
        return {
            "images": [{"name": m["name"], "url": m["url"], "mtime": int(m["mtime_ts"])}
                       for m in real],
            "extended": real,
            "roots_scanned": [],
            "total_found": len(real),
            "mock": True,
        }
    now = time.time()
    items = []
    for i, row in enumerate(r for r in _SAMPLE_OUTPUTS if r[1] == "image"):
        if len(items) >= limit:
            break
        name = f"{row[0]}.png"
        path = f"/outputs/{name}"
        items.append({
            "key": hashlib.md5(path.encode()).hexdigest()[:16],
            "name": name,
            "rel": name,
            "root": "/outputs",
            "url": f"/thumb?path={path}",
            "mtime": int(now - i * 3_600),
            "mock": True,
        })
    return {
        "images": [{"name": i["name"], "url": i["url"], "mtime": i["mtime"]} for i in items],
        "extended": items,
        "roots_scanned": [],
        "total_found": len(items),
        "mock": True,
    }


def upstream(path: str, payload: dict | None = None) -> tuple[int, dict[str, Any]] | None:
    """Return a recorded response for the render backend, or ``None``.

    ``None`` means "I cannot simulate this route": the caller then falls back
    to an explicit 503 rather than a fake success.

    Parameters
    ----------
    path : str
        Path requested on the upstream backend.
    payload : dict, optional
        Request body, used to derive a stable job id.

    Returns
    -------
    tuple of (int, dict) or None
        HTTP status code and JSON payload.
    """
    time.sleep(LATENCY_UPSTREAM)
    route = path.split("?")[0].rstrip("/")
    seed = hashlib.sha1(
        (route + str(sorted((payload or {}).items()))).encode("utf-8", "replace")
    ).hexdigest()[:8]

    if route.endswith("/generate") or route.endswith("/render"):
        return 200, {"job_id": f"mock-{seed}", "status": "queued", "mock": True}
    if route.endswith("/status") or route.endswith("/progress"):
        return 200, {
            "job_id": f"mock-{seed}",
            "status": "done",
            "progress": 1.0,
            "step": 12,
            "total_steps": 12,
            "seconds_per_step": 12.0,
            "output": "/outputs/mock-clip.mp4",
            "mock": True,
        }
    if route.endswith("/outputs"):
        return 200, outputs()
    if route.endswith("/sidecar"):
        # GET /sidecar?path=… — the path travels in the query, not the body.
        query = parse_qs(urlparse(path).query)
        return 200, sidecar(query.get("path", [""])[0])
    if route.endswith("/queue") or route.endswith("/jobs"):
        return 200, {"jobs": [], "mock": True}
    if route.endswith("/models"):
        return 200, {"models": ["ltx-2.3-mock"], "current": "ltx-2.3-mock", "mock": True}
    return None

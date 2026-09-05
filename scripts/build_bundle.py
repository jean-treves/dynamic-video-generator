"""Build a standalone single-file SPA from index.html plus its sub-pages.

The sub-pages (generator, storyboard, gallery) are embedded as base64 and
injected into the iframes through `srcdoc` (same origin as the parent, so
localStorage, CORS fetch and postMessage keep working). The render backend is
still discovered through Firebase (Cloudflare tunnel), so the hosted file
drives the render machine remotely.

Usage: python build_bundle.py  ->  writes dynamic_video_generator_app.html
"""

from __future__ import annotations

import base64
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1] / "web"
INDEX = ROOT / "index.html"
PAGES = {
    "generator": ROOT / "generator.html",
    "storyboard": ROOT / "storyboard.html",
    "gallery": ROOT / "gallery.html",
}
OUT = ROOT / "dynamic_video_generator_app.html"


def _b64(path: Path) -> str:
    """Encode a file as ASCII base64 (UTF-8 source)."""
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    """Replace `old` with `new`, failing loudly if the anchor is missing or ambiguous."""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"anchor '{label}' found {n} times (expected 1) — has index.html changed?")
    return text.replace(old, new)


def build() -> None:
    if not INDEX.is_file():
        raise SystemExit(f"index.html introuvable : {INDEX}")
    html = INDEX.read_text(encoding="utf-8")

    # 1) Page-loading block: files/onFile/pageURL -> base64 srcdoc.
    html = _replace_once(
        html,
        "const PAGE_FILES={generator:'generator.html',storyboard:'storyboard.html',gallery:'gallery.html'};\n"
        "const onFile=location.protocol==='file:';\n"
        "const pageURL=v=>onFile?PAGE_FILES[v]:base()+'/'+v;",
        "const onFile=location.protocol==='file:';\n"
        "function b64utf8(b64){const bin=atob(b64);const u=Uint8Array.from(bin,c=>c.charCodeAt(0));return new TextDecoder().decode(u);}\n"
        "function loadPage(v){const f=document.getElementById('f-'+v);if(!f||f.dataset.loaded)return;const b=(window.__PAGES__||{})[v];if(b){f.srcdoc=b64utf8(b);f.dataset.loaded='1';}}\n"
        "const pageURL=v=>v;",
        "bloc-chargement",
    )

    # 2) Tab switch: f.src=pageURL(v) -> loadPage(v) (srcdoc).
    html = _replace_once(
        html,
        " const f=document.getElementById('f-'+v);if(!f.src)f.src=pageURL(v);f.classList.add('active');",
        " loadPage(v);const f=document.getElementById('f-'+v);f.classList.add('active');",
        "tab-switch",
    )

    # 3) applyBK must NOT re-point the iframes (srcdoc pages discover their own
    #    backend through Firebase). Drop the src=BK+'/'+v repropagation.
    html = _replace_once(
        html,
        "function applyBK(u){if(!u)return;BK=u.replace(/\\/+$/,'');document.getElementById('ep').value=BK;if(!onFile)['generator','storyboard','gallery'].forEach(v=>{const f=document.getElementById('f-'+v);if(f.src)f.src=BK+'/'+v;});}",
        "function applyBK(u){if(!u)return;BK=u.replace(/\\/+$/,'');document.getElementById('ep').value=BK;}",
        "applyBK",
    )

    # 4) Initial load of the generator.
    html = _replace_once(
        html,
        "document.getElementById('f-generator').src=pageURL('generator');",
        "loadPage('generator');",
        "init-generator",
    )

    # 5) Inject the base64 pages just before `let BK=` (start of the main script).
    pages_js = "window.__PAGES__={" + ",".join(
        f'{k}:"{_b64(p)}"' for k, p in PAGES.items()
    ) + "};\n"
    anchor = "let BK=(/^https?:/.test(location.protocol)&&location.hostname&&location.port=='8200')"
    html = _replace_once(html, anchor, pages_js + anchor, "inject-pages")

    OUT.write_text(html, encoding="utf-8")
    size_kb = OUT.stat().st_size / 1024
    logger.info("✓ %s written (%.0f KB)", OUT.name, size_kb)
    for k, p in PAGES.items():
        logger.info("  · %-10s %5.0f Ko → base64", k, p.stat().st_size / 1024)


if __name__ == "__main__":
    try:
        build()
    except SystemExit as exc:
        logger.error("✗ %s", exc)
        sys.exit(1)

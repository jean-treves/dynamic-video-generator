"""Runtime configuration: environment, ports, models, CORS.

Everything here is read once at import time. Two values are mutated later and
must therefore be reached through this module (``config.MOCK_MODE``,
``config.OLLAMA_MODEL``) rather than imported by value: ``--mock`` flips the
first in :func:`~dynamic_video_generator.proxy.main`, and the second falls back
to an installed model when the configured one is missing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dynamic_video_generator import personas

#: Replay mode: no dependency on the render backend or the local LLM.
MOCK_MODE = os.environ.get("PHOS_MOCK", "").strip().lower() not in ("", "0", "false", "no")

#: Directory of the HTML pages served from the same origin as the API.
WEB_DIR = Path(
    os.environ.get("PHOS_WEB_DIR", Path(__file__).resolve().parents[2] / "web")
)

PROXY_VERSION = "v20"  # exposed via /health -> the UI warns when the render-host proxy is stale
PROXY_PORT = 8200
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8198
# Draw Things' local API. Reached via the /dt/* prefix so the UI hits it in the
# SAME origin as everything else (works over HTTPS/tunnel, no hardcoded :7861).
DRAWTHINGS_HOST = "127.0.0.1"
DRAWTHINGS_PORT = 7860


_MALLOC_ENV_VARS = {
    "MallocStackLogging",
    "MallocStackLoggingNoCompact",
    "MallocScribble",
    "MallocGuardEdges",
    "MallocNanoZone",
}
_SCRUBBED_MALLOC_ENV = {
    key: os.environ.pop(key)
    for key in _MALLOC_ENV_VARS
    if key in os.environ
}

_ENV_CANDIDATES: list[Path] = [
    Path(__file__).resolve().parent / ".env",  # next to the script
    Path.cwd() / ".env",                        # current directory
    Path.home() / ".env",                        # ~/.env
    Path.home() / "Desktop" / ".env",            # ~/Desktop/.env
]
_ENV_LOADED_FROM: list[str] = []


def _load_env() -> None:
    """Populate os.environ from any .env found in known locations.

    Looks in several locations (next to the script, cwd, home, Desktop) so
    that it works whatever the user or machine. No key is ever hardcoded.
    Environment variables already set take precedence.
    """
    seen: set[str] = set()
    for env_path in _ENV_CANDIDATES:
        if not env_path.is_file():
            continue
        real = str(env_path.resolve())
        if real in seen:        # script dir == ~/Desktop -> do not load twice
            continue
        seen.add(real)
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key in _MALLOC_ENV_VARS:
                continue
            os.environ.setdefault(key, val)
        _ENV_LOADED_FROM.append(str(env_path))


_load_env()
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
# Prompt rewriting is a light task: a 4B model is more than enough and loads
# ~5x faster using ~3 GB instead of ~8 GB (less RAM contention with the LTX
# render). Override with OLLAMA_MODEL in .env.
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "180"))
# How long Ollama keeps the model in RAM after a request. Short by default:
# gemma releases unified memory quickly so it does not compete with the LTX
# video render. Back-to-back rewrites (<30 s) reuse the warm model.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "0")
# Ultra-light model for generating during an LTX render (~1 GB RAM free).
OLLAMA_MODEL_LITE = os.environ.get("OLLAMA_MODEL_LITE", "gemma3:1b")

# System prompts and style packs: the engine hardcodes no universe.
# Prompts live in prompts/*.txt; a pack (PHOS_PACK) can add to or override
# them. See personas.py and packs/example/.
PROMPTS = personas.load()


HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# Phosphene's _is_local_request() rejects any request whose Origin header is
# present and non-loopback (and rejects `Origin: null` outright). A browser
# behind ngrok / file:// always sends such an Origin, so we must drop it (and
# Referer) before forwarding. With no Origin header, Phosphene's check passes
# on the loopback Host we set below. These are stripped IN ADDITION to the
# hop-by-hop set above.
STRIP_REQUEST_HEADERS = {"origin", "referer"}

# Headers dropped when relaying the UPSTREAM RESPONSE back to the browser.
# Crucially this does NOT include content-length / content-range: we stream the
# body 1:1, so those must pass through for the <video> tag to seek correctly.
RESPONSE_SKIP = {
    "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authenticate", "proxy-authorization",
    # We emit our own Server/Date via send_response(); relaying upstream's too
    # produces duplicate headers that ngrok's HTTP/2 layer can choke on.
    "server", "date",
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
    "Access-Control-Max-Age": "86400",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
#: Language of the spoken lines the /dialogue and /storyboard routes write.
#: The visual half of a prompt stays English — LTX reads English — but which
#: language the characters speak is a creative choice, not an engine constant.
DIALOGUE_LANG = os.environ.get("DIALOGUE_LANG", "French").strip() or "French"

logger = logging.getLogger("dvg-proxy")

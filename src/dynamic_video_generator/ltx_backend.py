"""
LTX-2-MLX direct backend — pure stdlib HTTP server that drives the
`ltx-2-mlx` CLI from https://github.com/dgrauet/ltx-2-mlx (the Apple-Silicon
MLX port), without going through the Phosphene wrapper.

WHY THE CLI AND NOT THE PYTHON API
----------------------------------
On a 16 GB M4 the binding constraint is unified memory, not speed. We launch
one `ltx-2-mlx generate` subprocess per job so the ~12 GB of MLX weights are
fully released the instant the render finishes — an in-process pipeline would
pin that memory for the lifetime of the server, leaving nothing for Safari.
The CLI is also the documented, stable interface (the repo's README is entirely
CLI examples), so we avoid guessing Python class signatures.

IMPORTANT — you must install the MLX FORK, not the official repo:
    git clone https://github.com/dgrauet/ltx-2-mlx.git   # packages end in -mlx
    cd ltx-2-mlx && uv sync --all-extras
    source .venv/bin/activate                             # activate THIS venv
    python3 /path/to/ltx_direct_backend.py
The official Lightricks/LTX-2 (packages: ltx-core, ltx-pipelines, ltx-trainer)
is CUDA-only and will never run on Apple Silicon.

REST surface (UI is served at / on the same origin):
    GET  /health           → backend status + whether the CLI was found
    POST /generate         → enqueue a job, returns {"job_id": "..."}
    GET  /jobs[/<id>]      → job status (+ live log tail)
    GET  /outputs/<name>   → serve a generated .mp4
    OPTIONS *              → CORS preflight

Run:
    python3 ltx_direct_backend.py
    # then open http://127.0.0.1:8197/

Env overrides (also the UI defaults):
    LTX_MODEL_DIR=dgrauet/ltx-2.3-mlx-q4   # int4 ~12 GB, the 16 GB-safe tier
    LTX_MODE=distilled                     # distilled|two-stage|two-stages-hq|one-stage
    LTX_LOW_RAM=1                          # stream blocks from disk (safer on 16 GB)
    LTX_BACKEND_PORT=8197
    LTX_OUTPUT_DIR=./ltx_outputs
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

PROXY_PORT = int(os.getenv("LTX_BACKEND_PORT", "8197"))
MODEL_DIR = os.getenv("LTX_MODEL_DIR", "dgrauet/ltx-2.3-mlx-q4")
MODE = os.getenv("LTX_MODE", "distilled")
LOW_RAM = os.getenv("LTX_LOW_RAM", "1") not in {"0", "", "false", "no"}
OUTPUT_DIR = Path(os.getenv("LTX_OUTPUT_DIR", "./ltx_outputs")).resolve()
# CLI mode flag → argv token. These mirror `ltx-2-mlx generate` exactly.
MODE_FLAGS = {
    "distilled": "--distilled",       # fastest, 16 GB sweet-spot
    "two-stage": "--two-stage",
    "two-stages-hq": "--two-stages-hq",
    "one-stage": "--one-stage",
}

# M4 16 GB-safe defaults.
DEFAULT_HEIGHT = 416
DEFAULT_WIDTH = 704
DEFAULT_NUM_FRAMES = 97  # must be 8k + 1
DEFAULT_SEED = 42
LOG_TAIL = 60  # lines of CLI output kept per job for the UI

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
    "Access-Control-Max-Age": "86400",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ltx-backend")


def _resolve_cli() -> list[str] | None:
    """Locate the `ltx-2-mlx` console script for the active environment.

    Returns the argv prefix to invoke it, or None if the MLX port is not
    installed in the venv this server runs under.
    """
    found = shutil.which("ltx-2-mlx")
    if found:
        return [found]
    candidate = Path(sys.prefix) / "bin" / "ltx-2-mlx"
    if candidate.exists():
        return [str(candidate)]
    # Last resort: the module is installed but the script isn't on PATH.
    try:
        __import__("ltx_pipelines_mlx")
        return [sys.executable, "-m", "ltx_pipelines_mlx.cli"]
    except ImportError:
        return None


CLI_CMD = _resolve_cli()


@dataclass
class Job:
    """One generation request and its lifecycle state."""

    id: str
    prompt: str
    height: int
    width: int
    num_frames: int
    seed: int
    mode: str
    model: str
    low_ram: bool
    image: str | None = None
    status: str = "pending"  # pending | running | done | error
    output_path: str | None = None
    error: str | None = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL))
    submitted_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        """Serialise for the wire — deque→list, add a relative output URL."""
        data = asdict(self)
        data["log"] = list(self.log)
        if self.output_path:
            data["output_url"] = f"/outputs/{Path(self.output_path).name}"
        return data


class JobRunner:
    """Single-worker queue that shells out to `ltx-2-mlx` once per job."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker = threading.Thread(target=self._loop, name="ltx-worker", daemon=True)
        self._worker.start()

    def submit(self, params: dict[str, Any]) -> Job:
        mode = str(params.get("mode") or MODE)
        if mode not in MODE_FLAGS:
            mode = MODE
        job = Job(
            id=time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            prompt=str(params["prompt"]).strip(),
            height=int(params.get("height", DEFAULT_HEIGHT)),
            width=int(params.get("width", DEFAULT_WIDTH)),
            num_frames=int(params.get("num_frames", DEFAULT_NUM_FRAMES)),
            seed=int(params.get("seed", DEFAULT_SEED)),
            mode=mode,
            model=str(params.get("model") or MODEL_DIR),
            low_ram=bool(params.get("low_ram", LOW_RAM)),
            image=params.get("image") or None,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self._queue.put(job.id)
        logger.info("queued %s (%s, %s, %d fr, %dx%d)",
                    job.id, job.mode, job.model.split("/")[-1], job.num_frames, job.width, job.height)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order)]

    def _build_argv(self, job: Job, output_path: Path) -> list[str]:
        assert CLI_CMD is not None
        argv = [
            *CLI_CMD, "generate",
            "--prompt", job.prompt,
            "--frames", str(job.num_frames),
            "--seed", str(job.seed),
            "-H", str(job.height),
            "-W", str(job.width),
            "--model", job.model,
            MODE_FLAGS[job.mode],
            "-o", str(output_path),
        ]
        if job.low_ram:
            argv.append("--low-ram")
        if job.image:
            argv += ["--image", job.image]
        return argv

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.get(job_id)
            if job is not None:
                self._run(job)

    def _run(self, job: Job) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = OUTPUT_DIR / f"{job.id}.mp4"
        with self._lock:
            job.status = "running"
            job.started_at = time.time()
            job.log.append(f"$ ltx-2-mlx generate ({job.mode}, {job.model.split('/')[-1]}, low-ram={job.low_ram})")
            job.log.append("loading model — first run downloads ~12 GB, then a few min to warm MLX…")
        argv = self._build_argv(job, output_path)
        logger.info("running %s: %s", job.id, " ".join(argv[len(CLI_CMD or []):]))

        # PYTHONUNBUFFERED so the CLI's stage logs reach us promptly.
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
        except OSError as exc:
            self._finish_error(job, f"cannot launch ltx-2-mlx: {exc}")
            return

        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                with self._lock:
                    job.log.append(line)
        code = proc.wait()

        if code == 0 and output_path.exists():
            with self._lock:
                job.status = "done"
                job.output_path = str(output_path)
                job.finished_at = time.time()
            elapsed = job.finished_at - (job.started_at or job.finished_at)
            logger.info("done %s in %.0fs → %s", job.id, elapsed, output_path.name)
        else:
            tail = "\n".join(list(job.log)[-6:])
            self._finish_error(job, f"ltx-2-mlx exited {code}.\n{tail}")

    def _finish_error(self, job: Job, msg: str) -> None:
        with self._lock:
            job.status = "error"
            job.error = msg
            job.finished_at = time.time()
        logger.error("job %s failed: %s", job.id, msg.splitlines()[0])


RUNNER = JobRunner()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_cors(self) -> None:
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json(404, {"error": f"not found: {path.name}"})
            return
        size = path.stat().st_size
        self.send_response(200)
        self._send_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    return

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._send_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html", "/studio", "/studio.html"}:
            self._send_json(200, {
                "service": "ltx-2-mlx backend",
                "ui": "none here — the studio is served by the dvg proxy",
                "routes": ["/health", "/jobs", "/jobs/<id>", "/outputs/<name>", "/generate"],
            })
            return
        if path == "/health":
            self._send_json(200, {
                "ok": True,
                "cli_found": CLI_CMD is not None,
                "cli_cmd": " ".join(CLI_CMD) if CLI_CMD else None,
                "model_dir": MODEL_DIR,
                "mode": MODE,
                "low_ram": LOW_RAM,
                "output_dir": str(OUTPUT_DIR),
                "active_jobs": sum(1 for j in RUNNER.list() if j.status in {"pending", "running"}),
            })
            return
        if path == "/jobs":
            self._send_json(200, {"jobs": [j.public() for j in RUNNER.list()]})
            return
        if path.startswith("/jobs/"):
            job = RUNNER.get(unquote(path[len("/jobs/"):]))
            self._send_json(200, job.public()) if job else self._send_json(404, {"error": "unknown job"})
            return
        if path.startswith("/outputs/"):
            name = unquote(path[len("/outputs/"):])
            if "/" in name or ".." in name:
                self._send_json(400, {"error": "bad filename"})
                return
            self._send_file(OUTPUT_DIR / name, "video/mp4")
            return
        self._send_json(404, {"error": f"no route: {path}"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return
        if path == "/generate":
            if CLI_CMD is None:
                self._send_json(503, {"error":
                    "ltx-2-mlx CLI not found. Activate the dgrauet/ltx-2-mlx venv "
                    "(uv sync --all-extras) before starting this backend — the official "
                    "Lightricks/LTX-2 is CUDA-only and won't work here."})
                return
            prompt = (payload.get("prompt") or "").strip()
            if not prompt:
                self._send_json(400, {"error": "prompt is required"})
                return
            num_frames = int(payload.get("num_frames", DEFAULT_NUM_FRAMES))
            if num_frames < 9 or (num_frames - 1) % 8 != 0:
                self._send_json(400, {"error": "num_frames must be 8k + 1 (e.g. 49, 97, 121)"})
                return
            job = RUNNER.submit(payload)
            self._send_json(202, {"job_id": job.id, "job": job.public()})
            return
        self._send_json(404, {"error": f"no route: {path}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug(fmt, *args)


def _free_port(port: int) -> None:
    """Kill any stale process holding the port so restart-the-script always wins."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True, text=True, timeout=5,
        ).stdout.split()
    except (FileNotFoundError, subprocess.SubprocessError):
        return
    me = os.getpid()
    for pid_s in out:
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == me:
            continue
        logger.info("killing stale process on port %d (pid %d)", port, pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if out:
        time.sleep(0.5)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _free_port(PROXY_PORT)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), Handler)
    except OSError as exc:
        logger.error("cannot bind port %d even after cleanup: %s", PROXY_PORT, exc)
        sys.exit(1)
    logger.info("=" * 60)
    logger.info("LTX-2-MLX direct backend")
    logger.info("listen   http://0.0.0.0:%d  (UI at /)", PROXY_PORT)
    if CLI_CMD:
        logger.info("cli      %s", " ".join(CLI_CMD))
    else:
        logger.warning("cli      NOT FOUND — install dgrauet/ltx-2-mlx and activate its venv")
    logger.info("mode     %s   model %s   low-ram %s", MODE, MODEL_DIR, LOW_RAM)
    logger.info("outputs  %s", OUTPUT_DIR)
    logger.info("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("stopping")
        server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()

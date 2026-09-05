"""Same-origin CORS proxy in front of the local generation backends.

The browser refuses to call LTX-Video, Draw Things and Ollama directly: each
listens on its own port and none of them send CORS headers. This proxy listens
on PROXY_PORT, serves the pages itself so everything shares a single origin,
and forwards the rest to the render backend on UPSTREAM_PORT.

Per-service behaviour lives in the mixins under ``backends/``; what stays here
is request routing, page serving and the health report.

    python3 -m dynamic_video_generator.proxy [--mock]
"""

from __future__ import annotations

import json
import http.client
import threading
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dynamic_video_generator import config, mock, tunnel
from dynamic_video_generator.backends.drawthings import DrawThingsMixin
from dynamic_video_generator.backends.media import MediaMixin
from dynamic_video_generator.backends.ollama import OllamaMixin
from dynamic_video_generator.config import (
    CORS_HEADERS,
    DRAWTHINGS_HOST,
    DRAWTHINGS_PORT,
    HOP_BY_HOP,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    PROMPTS,
    PROXY_PORT,
    PROXY_VERSION,
    RESPONSE_SKIP,
    STRIP_REQUEST_HEADERS,
    UPSTREAM_HOST,
    UPSTREAM_PORT,
    WEB_DIR,
    _ENV_CANDIDATES,
    _ENV_LOADED_FROM,
    logger,
)


class ProxyHandler(DrawThingsMixin, OllamaMixin, MediaMixin, BaseHTTPRequestHandler):
    """Routes every request, and serves the pages on the API's own origin."""

    # Safari's <video> loader fails range requests served over HTTP/1.0 with
    # "the network connection was lost". HTTP/1.1 (with correct Content-Length,
    # which we relay from upstream) is required for media streaming + seek.
    protocol_version = "HTTP/1.1"

    def _send_cors(self) -> None:
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._send_cors()
        self.send_header("Content-Length", "0")  # required for HTTP/1.1 keep-alive
        self.end_headers()
    def _forward(self) -> None:
        body_len = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(body_len) if body_len else b""

        if config.MOCK_MODE:
            # Render machine absent: replay a recorded response, or return
            # an explicit 503 rather than a fake success.
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                payload = {}
            canned = mock.upstream(self.path, payload if isinstance(payload, dict) else {})
            if canned is None:
                self._json_reply(503, {
                    "error": "render backend absent (mock mode)",
                    "path": self.path, "mock": True,
                })
            else:
                self._json_reply(*canned)
            return

        origin_in = self.headers.get("Origin")  # captured for the proof log

        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in STRIP_REQUEST_HEADERS
        }
        # Make Phosphene's _is_local_request() pass: loopback Host, no Origin.
        fwd_headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        fwd_headers.pop("Origin", None)  # belt-and-suspenders if casing slipped
        fwd_headers.pop("Referer", None)

        try:
            conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)
            conn.request(self.command, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, OSError) as exc:
            # The render machine can be gone for good. Rather than let every
            # screen read "unreachable" on an app that ships real renders in
            # its own tree, answer the listing from those — flagged, so nothing
            # pretends the backend is up.
            if self.path.split("?")[0] == "/outputs":
                demo = mock.demo_media()
                if demo:
                    logger.info("upstream down; /outputs served from docs/demo")
                    self._json_reply(200, {
                        "outputs": demo,
                        "backend": "offline",
                        "note": "render backend unreachable — showing the "
                                "renders committed under docs/demo/",
                    })
                    return
            msg = f"Phosphene unreachable at {UPSTREAM_HOST}:{UPSTREAM_PORT}\n{exc}".encode()
            self.send_response(502)
            self._send_cors()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            logger.error("upstream down: %s", exc)
            return

        self.send_response(resp.status)
        # Relay upstream headers, keeping Content-Length and Content-Range so the
        # browser's <video> tag gets well-formed (and seekable, for 206) responses.
        # Only drop true connection-level headers; re-emit our own CORS.
        has_clen = False
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in RESPONSE_SKIP or kl.startswith("access-control-"):
                continue
            if kl == "content-length":
                has_clen = True
            self.send_header(k, v)
        self._send_cors()
        # HTTP/1.1 keep-alive needs a framed body. Phosphene always sends
        # Content-Length, but if one is ever missing we must close the
        # connection so the browser knows the body ended at EOF.
        if not has_clen:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        # Stream the body in chunks instead of buffering the whole file in RAM —
        # essential for large MP4s and proper progressive video playback.
        sent = 0
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Browser hung up mid-stream (seeked / closed tab). Not an error,
            # but the connection is now half-written — don't reuse it.
            self.close_connection = True
            return
        finally:
            conn.close()

        # Proof log: what the browser sent vs what we forward to Phosphene.
        flag = "403←CHECK-FAILED" if resp.status == 403 else "ok"
        logger.info(
            "%s %s | origin_in=%r -> host_out=%r origin_out=%r | upstream=%d %s (%d B)",
            self.command, self.path, origin_in,
            fwd_headers.get("Host"), fwd_headers.get("Origin"),
            resp.status, flag, sent,
        )

    def do_POST(self) -> None:  # noqa: N802
        route = self.path.split("?")[0]
        if route == "/dt" or route.startswith("/dt/"):
            self._forward_dt()
            return
        if route == "/img-save":
            self._serve_img_save()
            return
        if route == "/storyboard":
            self._serve_storyboard()
            return
        if route == "/llm/sleep":
            self._serve_llm_sleep()
            return
        if route == "/assemble":
            self._serve_assemble()
            return
        if route == "/launcher":
            self._serve_launcher()
            return
        if route == "/caption":
            self._serve_caption()
            return
        if route == "/caption-video":
            self._serve_caption_video()
            return
        if route == "/websearch-image":
            self._serve_websearch_image()
            return
        if route == "/model":
            self._serve_set_model()
            return
        if route == "/chat":
            self._serve_chat()
            return
        transform = PROMPTS.transform(route)
        if transform is not None:
            self._serve_llm(transform)
            return
        self._forward()
    def _json_reply(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # never hardcodes a URL (works locally AND through the current tunnel).
    _PAGE_ROUTES = {
        "/": "index.html",
        "/generator": "generator.html",
        "/gallery": "gallery.html",
        "/storyboard": "storyboard.html",
        "/doc": "doc.html",
    }

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/dt" or path.startswith("/dt/"):
            self._forward_dt()
            return
        if path == "/thumb":
            self._serve_thumb()
            return
        if path.startswith("/demo/"):
            self._serve_demo(path[len("/demo/"):])
            return
        if config.MOCK_MODE and path.startswith("/outputs/"):
            # Replay mode owns no media files. Answering with the same labelled
            # placeholder the thumbnails use keeps every tile legible instead of
            # scattering 404s through the gallery.
            self._serve_thumb()
            return
        if path == "/img-list":
            self._serve_img_list()
            return
        if path == "/img-scan":
            self._serve_img_scan()
            return
        if path == "/img-file":
            self._serve_img_file()
            return
        if path in self._PAGE_ROUTES:
            self._serve_page(self._PAGE_ROUTES[path])
            return
        if path == "/health":
            self._serve_health()
            return
        if path == "/characters":
            # Trained characters of the active pack ({} with no pack): the
            # engine ships none of its own.
            self._json_reply(200, PROMPTS.characters or {})
            return
        if path == "/corpus":
            # Word banks + narrative presets of the active pack ({} with no pack).
            self._json_reply(200, PROMPTS.corpus or {})
            return
        if path == "/transforms":
            # The UI builds its buttons from here: the active pack's routes
            # appear, and disappear cleanly when no pack is loaded.
            self._json_reply(200, PROMPTS.describe())
            return
        if path == "/models":
            tags = []
            try:
                with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=4) as r:
                    tags = [m.get("name") for m in json.loads(r.read()).get("models", [])]
            except Exception:
                pass
            self._json_reply(200, {"models": tags, "current": config.OLLAMA_MODEL}); return
        if path == "/tunnel/url":
            url = tunnel._TUNNEL.get("url", "")
            if not url:
                try:
                    url = Path(tunnel.TUNNEL_URL_FILE).read_text().strip()
                except OSError:
                    url = ""
            self._json_reply(200, {"kind": tunnel._TUNNEL.get("kind", ""), "url": url})
            return
        self._forward()
    def _serve_page(self, filename: str) -> None:
        """Serve one of the HTML pages (generator/gallery/storyboard) from the
        web directory, on the same origin as the API."""
        page = WEB_DIR / filename
        try:
            body = page.read_bytes()
        except OSError:
            # Local file deleted (only index.html remains) -> forward to
            # Phosphene (upstream) instead of a 404, so the tab keeps working.
            logger.info("local page missing (%s) -> forwarding upstream %s", filename, self.path)
            self._forward(); return
        body = self._inject_client_config(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
    @staticmethod
    def _inject_client_config(body: bytes) -> bytes:
        """Inject the server configuration into the page, right after <head>.

        Pages hardcode neither the tunnel directory nor the pack name: the
        repository stays free of infrastructure identifiers, and the same page
        works for any deployment.

        Parameters
        ----------
        body : bytes
            HTML page read from disk.

        Returns
        -------
        bytes
            The page with a configuration ``<script>`` block, or the page
            unchanged if no ``<head>`` tag is found.
        """
        cfg = json.dumps({
            "tunnel.FIREBASE_DB": tunnel.FIREBASE_DB,
            "PHOS_PACK": PROMPTS.pack_id,
            "MOCK": config.MOCK_MODE,
            "DIALOGUE_LANG": config.DIALOGUE_LANG,
        })
        tag = f"<script>window.PHOS_CONFIG={cfg};Object.assign(window,window.PHOS_CONFIG);</script>".encode()
        marker = b"<head>"
        idx = body.find(marker)
        if idx == -1:
            return body
        pos = idx + len(marker)
        return body[:pos] + tag + body[pos:]
    def _serve_launcher(self) -> None:
        """Public tunnel control from the UI (without touching the proxy itself)."""
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            action = (json.loads(raw or b"{}").get("action") or "").strip()
        except json.JSONDecodeError:
            action = ""
        if action in ("tunnel_off", "off"):
            tunnel._kill_tunnels()
            tunnel._TUNNEL.update(kind="", url="")
            try:
                os.remove(tunnel.TUNNEL_URL_FILE)
            except OSError:
                pass
            logger.info("tunnel stopped (bandwidth saving)")
            self._json_reply(200, {"ok": True, "kind": "", "url": ""})
            return
        if action in ("cloudflare", "ngrok"):
            self._json_reply(200, tunnel._start_tunnel(action, PROXY_PORT))
            return
        if action == "stop_all":
            self._json_reply(200, {"ok": True, "stopping": True})
            tunnel._kill_tunnels()
            # kill the proxy after responding (full kill switch)
            threading.Thread(
                target=lambda: (time.sleep(0.5), os.kill(os.getpid(), 9)), daemon=True
            ).start()
            return
        self._json_reply(400, {"ok": False, "error": f"action inconnue: {action}"})

    do_PUT = do_DELETE = do_PATCH = _forward  # noqa: N815

    def _serve_demo(self, name: str) -> None:
        """Serve a committed demo render by file name.

        Only the flat contents of docs/demo/ are reachable: the name is
        reduced to its last path component, so no traversal can walk out.
        """
        safe = Path(name).name
        f = mock.DEMO_DIR / safe
        mime = {".mp4": "video/mp4", ".mov": "video/quicktime",
                ".webm": "video/webm", ".png": "image/png",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(f.suffix.lower())
        if not mime or not f.is_file():
            self.send_response(404); self._send_cors()
            self.send_header("Content-Length", "0"); self.end_headers(); return
        data = f.read_bytes()
        self.send_response(200); self._send_cors()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _serve_health(self) -> None:
        """Health of the whole stack, for the UI status dots."""
        def _up(host: str, port: int) -> bool:
            try:
                c = http.client.HTTPConnection(host, port, timeout=2)
                c.request("GET", "/"); c.getresponse(); c.close()
                return True
            except Exception:  # noqa: BLE001
                return False

        phosphene = _up(UPSTREAM_HOST, UPSTREAM_PORT)
        drawthings = _up(DRAWTHINGS_HOST, DRAWTHINGS_PORT)
        ollama, model_available, tags = False, None, []
        try:
            with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=2) as r:
                tags = [m.get("name") for m in json.loads(r.read()).get("models", [])]
            ollama = True
            model_available = config.OLLAMA_MODEL in tags
        except Exception:  # noqa: BLE001
            ollama = False
        sys_info = {}
        try:
            import subprocess as _sp, shutil as _sh
            vm = _sp.run(["vm_stat"], capture_output=True, text=True, timeout=3)
            if vm.returncode == 0:
                lines = vm.stdout.strip().split("\n")
                page_size = 16384
                for ln in lines:
                    if "page size" in ln.lower():
                        try:
                            page_size = int("".join(c for c in ln.split()[-1] if c.isdigit()))
                        except ValueError:
                            pass
                vals = {}
                for ln in lines[1:]:
                    parts = ln.split(":")
                    if len(parts) == 2:
                        key = parts[0].strip().lower().replace(" ", "_")
                        try:
                            vals[key] = int(parts[1].strip().rstrip("."))
                        except ValueError:
                            pass
                free_pages = vals.get("pages_free", 0) + vals.get("pages_speculative", 0)
                wired = vals.get("pages_wired_down", 0)
                active = vals.get("pages_active", 0)
                compressed = vals.get("pages_occupied_by_compressor", 0)
                total_cmd = _sp.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2)
                total_bytes = int(total_cmd.stdout.strip()) if total_cmd.returncode == 0 else 0
                used_bytes = (active + wired + compressed) * page_size
                free_bytes = total_bytes - used_bytes if total_bytes else free_pages * page_size
                sys_info["ram_total_gb"] = round(total_bytes / (1024 ** 3), 1)
                sys_info["ram_used_gb"] = round(used_bytes / (1024 ** 3), 1)
                sys_info["ram_free_gb"] = round(free_bytes / (1024 ** 3), 1)
                sys_info["ram_pct"] = round(used_bytes / total_bytes * 100, 1) if total_bytes else 0
            disk = _sh.disk_usage(str(Path.home()))
            sys_info["disk_free_gb"] = round(disk.free / (1024 ** 3), 1)
            sys_info["disk_total_gb"] = round(disk.total / (1024 ** 3), 1)
        except Exception:  # noqa: BLE001
            pass
        dt_roots = [str(r) for r in self._dt_all_roots() if r.is_dir()]
        self._json_reply(200, {
            "proxy": True, "version": PROXY_VERSION, "phosphene": phosphene, "ollama": ollama,
            "drawthings": drawthings,
            "model": config.OLLAMA_MODEL, "model_available": model_available,
            "models": tags,
            "system": sys_info,
            "dt_roots": dt_roots,
        })
    def log_message(self, *_a, **_kw) -> None:  # silence default access log
        pass


def _free_port(port: int) -> None:
    """Kill any process already listening on `port` so a restart always wins.

    This is what makes "just run the script again" reliable: a stale proxy
    holding the port (the v1 bug) is terminated before we bind, instead of the
    new process silently failing with EADDRINUSE and leaving old code running.
    """
    import signal

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
        time.sleep(0.5)  # let the kernel release the socket


def _resolve_model() -> None:
    """Check that config.OLLAMA_MODEL is installed; otherwise fall back to a light one.

    Avoids the 'model not found' error when the default model has not been
    pulled, preferring a small fast model that is already present.
    """
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=5) as r:
            tags = [m.get("name") for m in json.loads(r.read()).get("models", [])]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ollama unreachable at startup (%s) — model not verified", exc)
        return
    if config.OLLAMA_MODEL in tags:
        return
    for cand in ("gemma3:4b", "gemma3:1b", "qwen2.5:1.5b", "llama3.2:1b", "qwen2.5:0.5b", "gemma4:4b", "llama3.2:3b", "qwen2.5:3b", "gemma4:12b"):
        if cand in tags:
            logger.warning("model %s missing -> falling back to %s. Run `ollama pull %s` for your choice.",
                           config.OLLAMA_MODEL, cand, config.OLLAMA_MODEL)
            config.OLLAMA_MODEL = cand
            return
    if tags:
        logger.warning("model %s missing -> falling back to %s (first available)", config.OLLAMA_MODEL, tags[0])
        config.OLLAMA_MODEL = tags[0]
    else:
        logger.warning("no Ollama model installed. Run: ollama pull %s", config.OLLAMA_MODEL)


def main() -> None:
    if "--mock" in sys.argv[1:]:
        config.MOCK_MODE = True
    _free_port(PROXY_PORT)
    if not config.MOCK_MODE:
        _resolve_model()
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    except OSError as exc:
        logger.error("cannot bind port %d even after cleanup: %s", PROXY_PORT, exc)
        sys.exit(1)
    logger.info("=" * 60)
    logger.info("Dynamic Video Generator — proxy CORS  [v18 · SPA dashboard · /caption /websearch-image /models /model · light model · 16GB]")
    logger.info("UI served at http://127.0.0.1:%d/ (generator), /gallery, /storyboard", PROXY_PORT)
    if _ENV_LOADED_FROM:
        logger.info(".env loaded from: %s", ", ".join(_ENV_LOADED_FROM))
    else:
        logger.info(".env: NONE found (looked in: %s)",
                    ", ".join(str(p) for p in _ENV_CANDIDATES))
    if config.MOCK_MODE:
        logger.info("MOCK MODE: recorded responses, no render backend or LLM required")
    logger.info("style pack: %s", PROMPTS.pack_name or "none (bare engine)")
    logger.info("rewrite routes: %s", ", ".join(PROMPTS.routes()))
    logger.info("llm local: %s  model=%s  keep_alive=%s", OLLAMA_BASE_URL, config.OLLAMA_MODEL, OLLAMA_KEEP_ALIVE)
    logger.info("listen  http://0.0.0.0:%d", PROXY_PORT)
    logger.info("upstream http://%s:%d", UPSTREAM_HOST, UPSTREAM_PORT)
    logger.info("strips request headers: %s", ", ".join(sorted(STRIP_REQUEST_HEADERS)))
    logger.info("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("stopping")
        server.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()

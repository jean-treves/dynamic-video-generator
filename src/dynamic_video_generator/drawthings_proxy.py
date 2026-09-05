"""
Tiny CORS proxy for Draw Things' local HTTP API.

Draw Things' API server listens on 127.0.0.1 and emits no CORS headers, so a
browser served from any other origin (file://, ngrok, a hosted frontend) can't
call /sdapi/v1/txt2img directly: the preflight has no Access-Control-Allow-*
and the request carries a non-loopback Origin.

This proxy listens on PROXY_PORT, forwards every request to
http://127.0.0.1:UPSTREAM_PORT, strips the browser's Origin/Referer, and adds
permissive CORS headers on the way back. Because the proxy itself runs on the
same host as Draw Things, the upstream call is loopback -> never seen as remote.

Usage:
    python3 drawthings_cors_proxy.py
    # then point the client/HTML form at http://127.0.0.1:7861
    # or expose it:  ngrok http 7861        (tunnel the PROXY port, not 7860)
"""

import http.client
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROXY_PORT = 7861
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 7860  # Draw Things API server default
UPSTREAM_TIMEOUT = 600  # image generation is slow (SDXL on 16 GB unified memory)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# A browser behind ngrok / file:// always sends a non-loopback Origin (and
# rejects nothing on its own, but a strict local server would). Dropping Origin
# and Referer keeps the forwarded request indistinguishable from a same-host
# call. Stripped IN ADDITION to the hop-by-hop set above.
STRIP_REQUEST_HEADERS = {"origin", "referer"}

# Headers dropped when relaying the UPSTREAM RESPONSE back to the browser.
# Crucially this does NOT include content-length / content-range: we stream the
# body 1:1, so those must pass through for large base64 images / range reads.
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
logger = logging.getLogger("drawthings-proxy")


class ProxyHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 (with the Content-Length we relay from upstream) is required for
    # well-framed responses; HTTP/1.0 breaks large/streamed bodies in browsers.
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

        origin_in = self.headers.get("Origin")  # captured for the proof log

        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in STRIP_REQUEST_HEADERS
        }
        # Loopback Host, no Origin -> looks like a same-host call to Draw Things.
        fwd_headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
        fwd_headers.pop("Origin", None)  # belt-and-suspenders if casing slipped
        fwd_headers.pop("Referer", None)

        try:
            conn = http.client.HTTPConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, timeout=UPSTREAM_TIMEOUT
            )
            conn.request(self.command, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, OSError) as exc:
            msg = (
                f"Draw Things unreachable at {UPSTREAM_HOST}:{UPSTREAM_PORT}\n"
                f"Is the API server enabled (Advanced tab, port {UPSTREAM_PORT})?\n{exc}"
            ).encode()
            self.send_response(502)
            self._send_cors()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            logger.error("upstream down: %s", exc)
            return

        self.send_response(resp.status)
        # Relay upstream headers, keeping Content-Length/Content-Range so the
        # browser gets well-formed (and seekable, for 206) responses. Only drop
        # true connection-level headers; re-emit our own CORS.
        has_clen = False
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in RESPONSE_SKIP or kl.startswith("access-control-"):
                continue
            if kl == "content-length":
                has_clen = True
            self.send_header(k, v)
        self._send_cors()
        # HTTP/1.1 keep-alive needs a framed body. If a Content-Length is ever
        # missing we must close the connection so the browser knows EOF.
        if not has_clen:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        # Stream the body in chunks instead of buffering the whole payload in
        # RAM — base64-encoded images can be several MB.
        sent = 0
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Browser hung up mid-stream (closed tab). Not an error, but the
            # connection is now half-written — don't reuse it.
            self.close_connection = True
            return
        finally:
            conn.close()

        # Proof log: what the browser sent vs what we forward to Draw Things.
        flag = "403<-CHECK-FAILED" if resp.status == 403 else "ok"
        logger.info(
            "%s %s | origin_in=%r -> host_out=%r | upstream=%d %s (%d B)",
            self.command, self.path, origin_in,
            fwd_headers.get("Host"), resp.status, flag, sent,
        )

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward  # noqa: N815

    def log_message(self, *_a, **_kw) -> None:  # silence default access log
        pass


def _free_port(port: int) -> None:
    """Kill any process already listening on `port` so a restart always wins.

    This is what makes "just run the script again" reliable: a stale proxy
    holding the port is terminated before we bind, instead of the new process
    silently failing with EADDRINUSE and leaving old code running.
    """
    import signal
    import subprocess

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


def main() -> None:
    _free_port(PROXY_PORT)
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    except OSError as exc:
        logger.error("cannot bind port %d even after cleanup: %s", PROXY_PORT, exc)
        sys.exit(1)
    logger.info("=" * 60)
    logger.info("Draw Things CORS proxy  [HTTP/1.1 streaming · self-cleaning]")
    logger.info("listen   http://0.0.0.0:%d", PROXY_PORT)
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

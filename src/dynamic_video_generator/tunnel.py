"""Public tunnel control: start, stop, and publish the ephemeral URL.

A free Cloudflare quick tunnel gets a new hostname on every restart. Rather
than hardcoding it anywhere, the machine that owns the tunnel publishes it to a
Realtime DB and the browser reads it back on load.
"""

from __future__ import annotations

import json
import os
import re
import threading
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from dynamic_video_generator.config import logger

# Reserved ngrok domain, if you own one. Empty = ngrok picks a random URL.
NGROK_DOMAIN = os.environ.get("NGROK_DOMAIN", "")
TUNNEL_URL_FILE = "/tmp/phos_tunnel_url.txt"
_CF_LOG = "/tmp/phos_cloudflared.log"
_TUNNEL = {"kind": "", "url": ""}

# ---- Firebase Realtime Database bridge (tunnel URL directory) --------------
# Realtime DB acting as a directory for the (ephemeral) tunnel URL. Empty =
# automatic discovery is disabled and the endpoint stays manual.
FIREBASE_DB = os.environ.get("FIREBASE_DB", "").rstrip("/")


def _firebase_publish_url(tunnel_url: str) -> None:
    """Publish the tunnel URL to Firebase RTDB so the frontend can read it.

    Unauthenticated REST PUT — requires RTDB rules open for writing on
    /tunnel (see the README or the Firebase console).
    Best-effort: a failure never blocks startup.
    """
    try:
        data = json.dumps(tunnel_url).encode("utf-8")
        req = urllib.request.Request(
            f"{FIREBASE_DB}/tunnel/url.json",
            data=data,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read()
        logger.info("Firebase: URL published -> %s", tunnel_url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Firebase: publish failed (%s) — check the RTDB rules", exc)



def _kill_tunnels() -> None:
    for pat in ("ngrok http", "cloudflared tunnel"):
        try:
            subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass


def _start_tunnel(kind: str, port: int) -> dict:
    """Start ngrok or cloudflare (quick tunnel) and capture the public URL."""
    _kill_tunnels()
    try:
        os.remove(TUNNEL_URL_FILE)
    except OSError:
        pass
    if kind == "cloudflare":
        if not shutil.which("cloudflared"):
            return {"ok": False, "error": "cloudflared introuvable (brew install cloudflared)"}
        logf = open(_CF_LOG, "w")  # noqa: SIM115
        subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
                         stdout=logf, stderr=subprocess.STDOUT)
        _TUNNEL.update(kind="cloudflare", url="")

        def _watch_cf() -> None:
            rx = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
            for _ in range(60):
                try:
                    m = rx.search(Path(_CF_LOG).read_text(errors="replace"))
                    if m:
                        _TUNNEL["url"] = m.group(0)
                        Path(TUNNEL_URL_FILE).write_text(m.group(0))
                        logger.info("cloudflare tunnel: %s", m.group(0))
                        # Publish to Firebase for the remote frontend
                        _firebase_publish_url(m.group(0))
                        return
                except OSError:
                    pass
                time.sleep(1)
        threading.Thread(target=_watch_cf, daemon=True).start()
        return {"ok": True, "kind": "cloudflare", "url": "", "pending": True}
    if kind == "ngrok":
        if not shutil.which("ngrok"):
            return {"ok": False, "error": "ngrok introuvable"}
        # With no reserved domain ngrok assigns a random URL: we cannot
        # guess it here, the user reads it from the ngrok log.
        cmd = ["ngrok", "http", str(port)]
        if NGROK_DOMAIN:
            cmd.insert(2, "--domain=" + NGROK_DOMAIN)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not NGROK_DOMAIN:
            _TUNNEL.update(kind="ngrok", url="")
            return {"ok": True, "kind": "ngrok", "url": "", "pending": True}
        url = f"https://{NGROK_DOMAIN}"
        _TUNNEL.update(kind="ngrok", url=url)
        Path(TUNNEL_URL_FILE).write_text(url)
        return {"ok": True, "kind": "ngrok", "url": url}
    return {"ok": False, "error": f"tunnel inconnu: {kind}"}

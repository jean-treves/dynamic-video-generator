"""Draw Things: image-generation proxy and on-disk image discovery.

Draw Things listens on its own port with no CORS headers, and stores generated
images inside a sandbox container whose location depends on how the app was
installed. This mixin fronts its API on the same origin as the rest of the UI,
and locates the images across every known root.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dynamic_video_generator import config, mock
from dynamic_video_generator.config import (
    DRAWTHINGS_HOST,
    DRAWTHINGS_PORT,
    HOP_BY_HOP,
    RESPONSE_SKIP,
    STRIP_REQUEST_HEADERS,
    logger,
)


#: Directory names the render backend writes its output into. The scan is an
#: allowlist because the alternative does not hold: Draw Things runs sandboxed
#: and its container maps Data/Pictures onto the user's own ~/Pictures, so a
#: denylist of package extensions still let "Bibliothèque Photo Booth" through
#: and the Images tab served personal photos over the proxy — and over the
#: public tunnel. Only these names, and only what the operator names below.
_OUTPUT_DIR_NAMES = frozenset({
    "generated", "assets", "outputs", "dt_images", "txt2img", "img2img",
})

#: Directories the operator opts in explicitly, colon-separated in
#: DRAWTHINGS_IMAGE_ROOTS. Empty by default: this tool is reachable over a
#: tunnel, so it reads nothing from a home directory that was not named.
EXTRA_IMAGE_ROOTS: tuple[Path, ...] = tuple(
    Path(p).expanduser()
    for p in os.environ.get("DRAWTHINGS_IMAGE_ROOTS", "").split(":")
    if p.strip()
)

#: Depth below an output directory. Draw Things nests one project level at most.
_MAX_SCAN_DEPTH = 2


def is_scannable(path: Path, root: Path | None = None) -> bool:
    """Return whether ``path`` may be read as a render-backend output.

    Parameters
    ----------
    path : Path
        Candidate file found under a scanned root.
    root : Path, optional
        The root the walk started from, used for the depth budget.

    Returns
    -------
    bool
        True only when the file sits inside a directory the backend writes to,
        or inside one the operator named in ``DRAWTHINGS_IMAGE_ROOTS``.
    """
    parts = path.parts[:-1]
    if any(p.startswith(".") for p in parts):
        return False

    for opted in EXTRA_IMAGE_ROOTS:
        try:
            path.relative_to(opted)
            return True
        except ValueError:
            continue

    # Somewhere below the root there has to be a directory the backend owns.
    rel = parts
    if root is not None:
        try:
            rel = path.relative_to(root).parts[:-1]
        except ValueError:
            return False
    hit = next(
        (n for n, part in enumerate(rel) if part.lower() in _OUTPUT_DIR_NAMES),
        None,
    )
    if hit is None:
        return False
    return len(rel) - hit - 1 <= _MAX_SCAN_DEPTH


class DrawThingsMixin:
    """Serves ``/dt/*`` and the ``/img-*`` image routes."""

    # Roots to scan (first existing one wins). The order reflects where Draw
    # Things actually stores its Assets on macOS — several are tried to cover
    # standard installs plus a directory local to the proxy.
    _DT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic"}
    _DT_IMAGE_ROOTS = (
        # Local to the proxy (images persisted through /img-save)
        Path(__file__).resolve().parent / "dt_images",
        # Draw Things — emplacements macOS standards
        Path.home() / "Documents" / "DrawThings" / "Assets",
        Path.home() / "Documents" / "DrawThings" / "Generated",
        Path.home() / "Documents" / "DrawThings",
        Path.home() / "Pictures" / "DrawThings" / "Assets",
        Path.home() / "Pictures" / "DrawThings" / "Generated",
        Path.home() / "Pictures" / "DrawThings",
        Path.home() / "Movies" / "DrawThings",
        # Ancien Draw Things (legacy)
        Path.home() / "Documents" / "Draw Things" / "Assets",
        # Draw Things Mac App Store: sandbox container — this is WHERE the App
        # Store build keeps generated history (NOT ~/Documents). Cause number one
        # of "the whole render-host history is missing".
        Path.home() / "Library" / "Containers" / "com.liuliu.draw-things" / "Data" / "Documents",
        Path.home() / "Library" / "Containers" / "com.liuliu.draw-things" / "Data" / "Documents" / "Pictures",
        Path.home() / "Library" / "Containers" / "com.liuliu.draw-things" / "Data" / "Pictures",
    )

    def _forward_dt(self) -> None:
        """Proxy the /dt/* prefix to the local Draw Things API (port 7860).

        Lets Draw Things be reached from the SAME ORIGIN as the rest of the UI:
        no more hardcoded fetch to http://127.0.0.1:7861 (broken over
        HTTPS/tunnel or from another device). Image generation travels through
        the same tunnel as the Phosphene pages.
        """
        dt_path = self.path[3:]  # strip "/dt"
        if not dt_path.startswith("/"):
            dt_path = "/" + dt_path

        body_len = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(body_len) if body_len else b""

        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP and k.lower() not in STRIP_REQUEST_HEADERS
        }
        fwd_headers["Host"] = f"{DRAWTHINGS_HOST}:{DRAWTHINGS_PORT}"
        fwd_headers.pop("Origin", None)
        fwd_headers.pop("Referer", None)

        try:
            conn = http.client.HTTPConnection(
                DRAWTHINGS_HOST, DRAWTHINGS_PORT, timeout=600
            )
            conn.request(self.command, dt_path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except (ConnectionRefusedError, OSError) as exc:
            msg = (
                f"Draw Things unreachable at {DRAWTHINGS_HOST}:{DRAWTHINGS_PORT}\n"
                f"Enable its API server (Advanced tab, port {DRAWTHINGS_PORT}).\n{exc}"
            ).encode()
            self.send_response(502)
            self._send_cors()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            logger.error("draw things down: %s", exc)
            return

        self.send_response(resp.status)
        has_clen = False
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl in RESPONSE_SKIP or kl.startswith("access-control-"):
                continue
            if kl == "content-length":
                has_clen = True
            self.send_header(k, v)
        self._send_cors()
        if not has_clen:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

        sent = 0
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        finally:
            conn.close()

        logger.info("DT %s %s | upstream=%d (%d B)",
                    self.command, dt_path, resp.status, sent)
    def _serve_img_save(self) -> None:
        """Persist a generated image (base64) into <dir>/dt_images/ on the host."""
        import base64 as _b64
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            img = json.loads(raw or b"{}").get("image") or ""
        except json.JSONDecodeError:
            img = ""
        if img.startswith("data:") and "," in img:
            img = img.split(",", 1)[1]
        try:
            data = _b64.b64decode(img)
        except Exception:  # noqa: BLE001
            data = b""
        if not data:
            self._json_reply(400, {"error": "image vide"}); return
        d = Path(__file__).resolve().parent / "dt_images"
        d.mkdir(exist_ok=True)
        name = time.strftime("dt_%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png"
        (d / name).write_bytes(data)
        logger.info("img-save: %s (%d B)", name, len(data))
        self._json_reply(200, {"ok": True, "name": name, "url": "/img-file?name=" + name})
    @classmethod
    def _dt_all_roots(cls):
        """Static roots plus sandbox containers discovered by glob.

        Covers a different bundle id (e.g. mas vs direct) and the Group
        Container, without hardcoding the team identifier."""
        roots = list(cls._DT_IMAGE_ROOTS)
        for base in (Path.home() / "Library" / "Containers",
                     Path.home() / "Library" / "Group Containers"):
            if not base.is_dir():
                continue
            try:
                for c in base.glob("*"):
                    n = c.name.lower()
                    if not (("draw" in n and "things" in n) or "liuliu" in n):
                        continue
                    for sub in ("Data/Documents", "Data/Documents/Pictures",
                                "Data/Pictures", "Documents", "Pictures", ""):
                        d = (c / sub) if sub else c
                        if d.is_dir() and d not in roots:
                            roots.append(d)
            except OSError as exc:
                logger.warning("dt container glob: %s (%s)", base, exc)
        return roots
    def _dt_collect_images(self, roots=None, limit=200, max_per_root=400):
        """Scan the DrawThings roots recursively (one level deep).

        Returns a list sorted by mtime desc with url = /img-file?name=<key>.
        The "key" is a stable identifier (hash of the absolute path) that
        _serve_img_file uses to find the real file on disk."""
        import hashlib as _hl
        roots = roots or self._dt_all_roots()
        out = []
        seen = set()
        for root in roots:
            if not root.is_dir():
                continue
            try:
                # One level of subdirectories (Assets/Generated/Projects/...)
                # is enough — DrawThings does not nest deeper.
                files = []
                for p in root.rglob("*"):
                    if not p.is_file() or p.suffix.lower() not in self._DT_IMAGE_EXTS:
                        continue
                    # Refuses dot-directories, macOS packages and personal
                    # media stores, and caps how deep the walk goes.
                    if not is_scannable(p, root):
                        continue
                    files.append(p)
                # Tri local mtime desc
                files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for p in files[:max_per_root]:
                    key = _hl.md5(str(p.resolve()).encode("utf-8")).hexdigest()[:16]
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "key": key,
                        "name": p.name,
                        "rel": str(p.relative_to(root)) if p.is_relative_to(root) else p.name,
                        "root": str(root),
                        "abs_path": str(p),
                        "url": "/img-file?key=" + key,
                        "mtime": int(p.stat().st_mtime),
                        "size": p.stat().st_size,
                    })
                    if len(out) >= limit:
                        break
            except PermissionError as exc:
                logger.warning("dt scan permission: %s (%s)", root, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dt scan error %s: %s", root, exc)
            if len(out) >= limit:
                break
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return out
    def _dt_resolve_key(self, key: str):
        """Resolve the absolute Path of an image from its md5 key."""
        import hashlib as _hl
        for root in self._dt_all_roots():
            if not root.is_dir():
                continue
            try:
                for p in root.rglob("*"):
                    if not p.is_file() or p.suffix.lower() not in self._DT_IMAGE_EXTS:
                        continue
                    if any(part.startswith(".") for part in p.parts[:-1]):
                        continue
                    k = _hl.md5(str(p.resolve()).encode("utf-8")).hexdigest()[:16]
                    if k == key:
                        return p
            except Exception:  # noqa: BLE001
                continue
        return None
    def _serve_img_list(self) -> None:
        """List the DrawThings images found (legacy-compatible shape)."""
        if config.MOCK_MODE:
            self._json_reply(200, mock.images()); return
        imgs = self._dt_collect_images(limit=200)
        if not imgs:
            # Nothing the backend wrote, so nothing to show — but the tree
            # ships real frames, and an empty tab reads like a broken one.
            demo = [m for m in mock.demo_media() if m["kind"] == "image"]
            if demo:
                self._json_reply(200, {
                    "images": [{"name": m["name"], "url": m["url"],
                                "mtime": int(m["mtime_ts"])} for m in demo],
                    "extended": demo,
                    "roots_scanned": [],
                    "total_found": len(demo),
                    "source": "docs/demo",
                })
                return
        # Legacy shape kept: [{name, url, mtime}]
        legacy = [{"name": i["name"], "url": i["url"], "mtime": i["mtime"]} for i in imgs]
        self._json_reply(200, {
            "images": legacy,
            "extended": imgs,           # richer detail for the newer UI
            "roots_scanned": [str(r) for r in self._dt_all_roots() if r.is_dir()],
            "total_found": len(imgs),
        })
    def _serve_img_scan(self) -> None:
        """Wide scan: return every DrawThings image with its provenance.

        If the hardcoded roots find nothing, fall back to mdfind (Spotlight)
        to discover where Draw Things actually stores its images."""
        if config.MOCK_MODE:
            self._json_reply(200, mock.images(400)); return
        qs = parse_qs(urlparse(self.path).query)
        try:
            limit = int((qs.get("limit") or ["400"])[0])
        except ValueError:
            limit = 400

        all_roots = self._dt_all_roots()
        existing = [r for r in all_roots if r.is_dir()]
        imgs = self._dt_collect_images(roots=existing, limit=limit, max_per_root=800)

        # Spotlight fallback: if the roots yield nothing, ask mdfind
        # to locate the DrawThings images across the whole disk.
        spotlight_roots = []
        if not imgs:
            try:
                import subprocess as _sp
                out = _sp.run(
                    ["mdfind", "-name", "DrawThings", "kMDItemContentType == public.folder"],
                    capture_output=True, text=True, timeout=10,
                )
                for line in out.stdout.strip().split("\n"):
                    d = Path(line.strip())
                    if d.is_dir() and d not in existing:
                        spotlight_roots.append(d)
                        existing.append(d)
                if spotlight_roots:
                    logger.info("img-scan Spotlight fallback: %s", spotlight_roots)
                    imgs = self._dt_collect_images(roots=spotlight_roots, limit=limit, max_per_root=800)
            except Exception as exc:  # noqa: BLE001
                logger.warning("img-scan mdfind fallback: %s", exc)

        by_root = {}
        for i in imgs:
            by_root.setdefault(i["root"], 0)
            by_root[i["root"]] += 1
        self._json_reply(200, {
            "images": imgs,
            "count": len(imgs),
            "by_root": by_root,
            "roots_scanned": [str(r) for r in existing],
            "roots_from_spotlight": [str(r) for r in spotlight_roots],
        })
    def _serve_img_file(self) -> None:
        """Serve an image by md5 key (multi-root scan) or by name
        (legacy compatibility: looks in dt_images/<name>)."""
        qs = parse_qs(urlparse(self.path).query)
        key = (qs.get("key") or [""])[0]
        name = (qs.get("name") or [""])[0]

        # Security: no directory traversal
        if key and re.fullmatch(r"[0-9a-f]{16}", key):
            f = self._dt_resolve_key(key)
        elif name and Path(name).name == name:
            f = Path(__file__).resolve().parent / "dt_images" / Path(name).name
            if not f.is_file():
                f = None
        else:
            f = None

        if not f or not f.is_file():
            self.send_response(404); self._send_cors()
            self.send_header("Content-Length", "0"); self.end_headers(); return
        # Inferred MIME type
        ext = f.suffix.lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "heic": "image/heic"}.get(ext.lstrip("."), "application/octet-stream")
        try:
            data = f.read_bytes()
        except Exception:  # noqa: BLE001
            self.send_response(500); self._send_cors()
            self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(200); self._send_cors()
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

"""ffmpeg-backed media routes: clip thumbnails and final assembly."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.request
import shutil
import subprocess
from pathlib import Path

from dynamic_video_generator import config, mock
from dynamic_video_generator.config import logger


class MediaMixin:
    """Serves ``/thumb`` and ``/assemble``."""

    def _serve_thumb(self) -> None:
        """JPEG thumbnail (first frame) of an /outputs video, for the gallery.

        ffmpeg extracts a frame at ~0.5 s and caches it in /tmp. The black tile
        in the gallery/dashboard shows this preview without downloading the
        whole clip.
        """
        import shutil
        import subprocess
        import tempfile
        from urllib.parse import parse_qs, urlparse

        p = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
        # A committed demo render is a real file, so ffmpeg can pull a real
        # frame from it. The drawn placeholder is only for a recorded entry
        # that stands for a render nobody has.
        demo = mock.DEMO_DIR / Path(p).name if p.startswith("/demo/") else None
        if demo is not None and demo.is_file():
            p = str(demo)
        elif config.MOCK_MODE:
            body = mock.poster(p or urlparse(self.path).path)
            self.send_response(200); self._send_cors()
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        src = Path(p)
        if not p or not src.is_file() or src.suffix.lower() not in (
                ".mp4", ".mov", ".webm", ".m4v", ".mkv"):
            self.send_response(404); self._send_cors()
            self.send_header("Content-Length", "0"); self.end_headers(); return

        ffmpeg = shutil.which("ffmpeg") or next(
            (q for q in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                         "/opt/anaconda3/bin/ffmpeg") if Path(q).exists()), None)
        cache = Path(tempfile.gettempdir()) / (
            "phos_thumb_" + hashlib.md5(
                f"{p}:{src.stat().st_mtime_ns}".encode()).hexdigest() + ".jpg")
        if ffmpeg and not cache.exists():
            try:
                subprocess.run(
                    [ffmpeg, "-y", "-ss", "0.5", "-i", str(src), "-frames:v", "1",
                     "-vf", "scale=480:-1", str(cache)],
                    capture_output=True, timeout=20, check=True)
            except (subprocess.SubprocessError, OSError) as exc:
                logger.info("thumb ffmpeg failed (%s): %s", src.name, exc)

        try:
            data = cache.read_bytes()
        except OSError:
            self.send_response(404); self._send_cors()
            self.send_header("Content-Length", "0"); self.end_headers(); return
        self.send_response(200); self._send_cors()
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
    def _serve_assemble(self) -> None:
        """Concatenate MP4 clips (same directory) into a single MP4 via ffmpeg.

        The audio (French voice-over muxed by LTX) is already in each clip, so
        this is a plain in-order concatenation. The result is written next to
        the clips (mlx_outputs) so it shows up in the gallery.
        """
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req = {}
        clips: list[Path] = []
        for p in (req.get("paths") or []):
            try:
                pp = Path(p).resolve()
            except Exception:  # noqa: BLE001
                continue
            if pp.is_file() and pp.suffix.lower() == ".mp4":
                clips.append(pp)
        if not clips:
            self._json_reply(400, {"error": "no valid MP4 clip"}); return
        out_dir = clips[0].parent
        if not all(c.parent == out_dir for c in clips):
            self._json_reply(400, {"error": "clips must live in the same directory"}); return

        ffmpeg = shutil.which("ffmpeg") or next(
            (p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                         "/opt/anaconda3/bin/ffmpeg") if Path(p).exists()), None)
        if not ffmpeg:
            self._json_reply(500, {"error": "ffmpeg not found on the server"}); return

        out = out_dir / f"storyboard_{int(time.time())}.mp4"
        listfile = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                             encoding="utf-8") as lf:
                for c in clips:
                    lf.write("file '%s'\n" % c.as_posix().replace("'", "'\\''"))
                listfile = lf.name

            def _run(extra: list[str]) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", listfile, *extra, str(out)],
                    capture_output=True, text=True, timeout=600,
                )
            # 1) try stream-copy (fast, lossless); 2) robust re-encode
            r = _run(["-c", "copy"])
            if r.returncode != 0 or not out.exists():
                r = _run(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                          "-c:a", "aac", "-b:a", "192k"])
        except subprocess.SubprocessError as exc:
            self._json_reply(500, {"error": f"ffmpeg: {exc}"}); return
        finally:
            if listfile:
                try:
                    os.unlink(listfile)
                except OSError:
                    pass

        if r.returncode != 0 or not out.exists():
            self._json_reply(500, {"error": "ffmpeg failed",
                                   "detail": (r.stderr or "")[-400:]}); return
        logger.info("ASSEMBLE %d clips -> %s", len(clips), out.name)
        self._json_reply(200, {
            "ok": True, "count": len(clips), "name": out.name, "output": str(out),
            "url": f"/file?path={urllib.parse.quote(str(out))}&v={int(time.time())}",
        })

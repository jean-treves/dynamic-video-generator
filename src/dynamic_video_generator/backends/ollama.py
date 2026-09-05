"""Local LLM: prompt rewriting, chat, storyboards and dataset captions.

Every rewrite route follows one contract — POST ``{prompt}`` returns
``{prompt}`` — so the interface can build its buttons from ``GET /transforms``
without knowing what any individual transform does.
"""

from __future__ import annotations

import base64 as _b64
import json
import http.client
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dynamic_video_generator import config, mock, personas
from dynamic_video_generator.config import (
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL_LITE,
    OLLAMA_TIMEOUT,
    PROMPTS,
    UPSTREAM_HOST,
    UPSTREAM_PORT,
    logger,
)

# JSON schema passed to Ollama structured outputs — forces the panel array shape
# (the reliable equivalent of Gemini's responseSchema, lost in the migration).
STORYBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "panels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "voiceover": {"type": "string"},
                },
                "required": ["prompt", "voiceover"],
            },
        },
    },
    "required": ["panels"],
}

def _extract_panels(parsed: object) -> list | None:
    """Normalise whatever JSON the LLM returned into a list of panel dicts.

    Handles: a bare list, {"panels":[...]}, the first list-valued field of a
    dict, or a dict keyed by index ({"1":{...},"2":{...}}).
    """
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        if isinstance(parsed.get("panels"), list):
            return parsed["panels"]
        for v in parsed.values():               # first list value wins
            if isinstance(v, list):
                return v
        # dict keyed by index → order by key
        items = [parsed[k] for k in sorted(parsed, key=lambda x: str(x))
                 if isinstance(parsed[k], dict)]
        if items:
            return items
    return None


class OllamaMixin:
    """Serves the prompt-rewriting, chat, storyboard and caption routes."""

    def _serve_llm_sleep(self) -> None:
        """Unload the Ollama model NOW (keep_alive=0) to free unified memory for
        the LTX video render. The model reloads on demand on the next rewrite."""
        # drain any request body so the connection stays clean
        body_len = int(self.headers.get("Content-Length") or 0)
        if body_len:
            self.rfile.read(body_len)
        payload = {"model": config.OLLAMA_MODEL, "keep_alive": 0}
        try:
            req = urllib.request.Request(
                f"{OLLAMA_BASE_URL}/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
            logger.info("LLM sleep: %s unloaded (RAM freed for rendering)", config.OLLAMA_MODEL)
            self._json_reply(200, {"ok": True, "unloaded": config.OLLAMA_MODEL})
        except Exception as exc:  # noqa: BLE001
            logger.error("LLM sleep failed: %s", exc)
            self._json_reply(200, {"ok": False, "error": str(exc)})  # best-effort
    def _serve_caption(self) -> None:
        """Vision: read the image (base64) and write a motion i2v prompt."""
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            req_obj = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req_obj = {}
        img = req_obj.get("image") or ""
        if "," in img and img.strip().startswith("data:"):
            img = img.split(",", 1)[1]
        if not img:  # no base64 -> try a server path (web or local image)
            p = (req_obj.get("path") or "").strip()
            if p and Path(p).is_file():
                import base64 as _b64
                img = _b64.b64encode(Path(p).read_bytes()).decode()
        if not img:
            self._json_reply(400, {"error": "image manquante (ni base64 ni chemin valide)"}); return
        sys_p = (
            "You are writing an IMAGE-TO-VIDEO prompt for LTX-Video 2.3 from this reference image. "
            "STEP 1 — LOOK: identify what is ACTUALLY in the image: the main subject(s) and their "
            "appearance (clothing, colors, materials, pose), the setting/background, the art style "
            "and the lighting. "
            "STEP 2 — WRITE: output ONE English paragraph of 50-100 words that first names that "
            "subject and scene concretely, THEN describes plausible motion starting from this still: "
            "how the subject moves, ONE clear camera move (slow push-in, pan, orbit, or tilt), and "
            "how light and atmosphere evolve. "
            "Stay strictly faithful to the image — do NOT invent objects, characters, or on-screen "
            "text that are not visible. No markdown, no lists, no preamble, no repetition. "
            "Output only the prompt.")
        payload = {"model": config.OLLAMA_MODEL, "prompt": sys_p, "images": [img], "stream": False,
                   "keep_alive": OLLAMA_KEEP_ALIVE,
                   "options": {"temperature": 0.45, "num_predict": 480, "repeat_penalty": 1.25}}
        try:
            req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
                data = json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            self._json_reply(502, {"error": f"vision LLM: {type(exc).__name__}: {exc} (multimodal model? gemma3/gemma4)"}); return
        txt = (data.get("response") or "").strip()
        self._json_reply(200, {"prompt": txt, "model": config.OLLAMA_MODEL})
    def _serve_caption_video(self) -> None:
        """Multi-frame vision: extract 3 frames (10/50/90 %) from a video and
        generate ONE descriptive caption, to prepare a LoRA dataset. Input is
        {path}. A single Ollama pass over the 3 frames (gemma3 multimodal)."""
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            req_obj = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req_obj = {}
        p = (req_obj.get("path") or "").strip()
        src = Path(p)
        if not p or not src.is_file() or src.suffix.lower() not in (
                ".mp4", ".mov", ".webm", ".m4v", ".mkv"):
            self._json_reply(400, {"error": "video not found (invalid server path)"}); return
        ffmpeg = shutil.which("ffmpeg") or next(
            (q for q in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                         "/opt/anaconda3/bin/ffmpeg") if Path(q).exists()), None)
        if not ffmpeg:
            self._json_reply(500, {"error": "ffmpeg not found on the server"}); return
        # Duration via ffprobe -> 3 timestamps. Fixed positions if unavailable.
        duration = 0.0
        ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
        try:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(src)],
                capture_output=True, text=True, timeout=10)
            duration = float(out.stdout.strip())
        except (subprocess.SubprocessError, OSError, ValueError):
            duration = 0.0
        if duration > 0:
            timestamps = [duration * 0.1, duration * 0.5, duration * 0.9]
        else:
            timestamps = [0.5, 1.0, 1.5]
        frames_b64 = []
        with tempfile.TemporaryDirectory() as td:
            for i, ts in enumerate(timestamps):
                fp = Path(td) / f"f{i}.jpg"
                try:
                    subprocess.run(
                        [ffmpeg, "-y", "-ss", f"{ts:.2f}", "-i", str(src),
                         "-frames:v", "1", "-vf", "scale=512:-1", str(fp)],
                        capture_output=True, timeout=25, check=True)
                    if fp.is_file():
                        frames_b64.append(_b64.b64encode(fp.read_bytes()).decode())
                except (subprocess.SubprocessError, OSError) as exc:
                    logger.info("caption-video frame %d failed: %s", i, exc)
        if not frames_b64:
            self._json_reply(500, {"error": "frame extraction failed (ffmpeg)"}); return
        if config.MOCK_MODE:
            self._json_reply(200, {"caption": mock.caption(str(src)), "frames": len(frames_b64),
                                   "duration_sec": round(duration, 1), "model": config.OLLAMA_MODEL,
                                   "mock": True})
            return
        sys_p = PROMPTS.prompt("caption_video")
        payload = {"model": config.OLLAMA_MODEL, "prompt": sys_p, "images": frames_b64, "stream": False,
                   "keep_alive": OLLAMA_KEEP_ALIVE,
                   "options": {"temperature": 0.4, "num_predict": 160, "repeat_penalty": 1.2}}
        try:
            req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
                data = json.loads(r.read())
        except Exception as exc:  # noqa: BLE001
            self._json_reply(502, {"error": f"vision LLM: {type(exc).__name__}: {exc} (multimodal model? gemma3/gemma4)"}); return
        txt = (data.get("response") or "").strip().strip('"')
        self._json_reply(200, {"caption": txt, "frames": len(frames_b64),
                               "duration_sec": round(duration, 1), "model": config.OLLAMA_MODEL})
    def _serve_websearch_image(self) -> None:
        """Find a free image (Openverse) and upload it to Phosphene -> i2v path."""
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            q = (json.loads(raw or b"{}").get("query") or "").strip()
        except json.JSONDecodeError:
            q = ""
        if not q:
            self._json_reply(400, {"error": "query vide"}); return
        try:
            url = ("https://api.openverse.org/v1/images/?page_size=8&q="
                   + urllib.parse.quote(q))
            req = urllib.request.Request(url, headers={"User-Agent": "phosphene/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                results = json.loads(r.read()).get("results", [])
        except Exception as exc:  # noqa: BLE001
            self._json_reply(502, {"error": f"Openverse: {exc}"}); return
        for res in results:
            src = res.get("url") or res.get("thumbnail")
            if not src:
                continue
            try:
                ir = urllib.request.Request(src, headers={"User-Agent": "phosphene/1.0"})
                with urllib.request.urlopen(ir, timeout=20) as im:
                    img_bytes = im.read()
                if len(img_bytes) < 1024:
                    continue
                path = self._phosphene_upload(img_bytes)
                self._json_reply(200, {"ok": True, "path": path, "source": src,
                                       "title": res.get("title", ""), "thumb": res.get("thumbnail", src)})
                return
            except Exception:  # noqa: BLE001
                continue
        self._json_reply(502, {"error": "no downloadable image found"})
    def _phosphene_upload(self, img_bytes: bytes) -> str:
        """Loopback multipart POST to Phosphene /upload, returns the server path."""
        boundary = "----phosWeb" + str(int(time.time() * 1000))
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
            f"filename=\"web.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n"
        ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
        conn = http.client.HTTPConnection(UPSTREAM_HOST, UPSTREAM_PORT, timeout=60)
        conn.request("POST", "/upload", body=body, headers={
            "Host": f"{UPSTREAM_HOST}:{UPSTREAM_PORT}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        })
        data = json.loads(conn.getresponse().read()); conn.close()
        if not data.get("path"):
            raise RuntimeError(str(data)[:200])
        return data["path"]
    def _serve_set_model(self) -> None:
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            m = (json.loads(raw or b"{}").get("model") or "").strip()
        except json.JSONDecodeError:
            m = ""
        if not m:
            self._json_reply(400, {"error": "model manquant"}); return
        globals()["config.OLLAMA_MODEL"] = m
        logger.info("LLM model switched -> %s", m)
        self._json_reply(200, {"ok": True, "model": m})
    def _serve_chat(self) -> None:
        """Free-form chat with the local LLM (Ollama /api/chat, keeps history)."""
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req = {}
        history = req.get("messages")
        if not isinstance(history, list):
            msg = (req.get("message") or req.get("prompt") or "").strip()
            history = [{"role": "user", "content": msg}] if msg else []
        if not history:
            self._json_reply(400, {"error": "message vide"}); return
        messages = [{"role": "system", "content": PROMPTS.prompt("chat")}] + [
            {"role": m.get("role", "user"), "content": str(m.get("content", ""))}
            for m in history if m.get("content")
        ]
        payload = {
            "model": config.OLLAMA_MODEL, "messages": messages, "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {"temperature": 0.7, "num_predict": 700,
                        "repeat_penalty": 1.3, "top_p": 0.9},
        }
        t0 = time.monotonic()
        try:
            r = urllib.request.Request(
                f"{OLLAMA_BASE_URL}/api/chat", data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(r, timeout=OLLAMA_TIMEOUT) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            self._json_reply(502, {"error": f"Ollama HTTP {exc.code}: {exc.read().decode('utf-8','replace')[:200]}"}); return
        except Exception as exc:  # noqa: BLE001
            self._json_reply(502, {"error": f"{type(exc).__name__}: {exc}"}); return
        reply = ((data.get("message") or {}).get("content") or "").strip()
        self._json_reply(200, {"reply": reply, "model": config.OLLAMA_MODEL,
                               "elapsed_sec": round(time.monotonic() - t0, 2)})
    def _ollama_generate(
        self,
        system: str,
        user_prompt: str,
        *,
        max_tokens: int = 900,
        temperature: float = 0.7,
        json_output: bool = False,
        json_schema: dict | None = None,
        repeat_penalty: float = 1.3,
        model: str | None = None,
        keep_alive: object | None = None,
        mock_tag: str = "",
    ) -> tuple[str, dict]:
        """Call local Ollama Gemma and return (text, timing/usage metadata)."""
        if config.MOCK_MODE:
            if json_output:
                return json.dumps({"panels": mock.storyboard(4)}), {
                    "duration_sec": mock.LATENCY_LLM, "model": config.OLLAMA_MODEL, "mock": True,
                }
            return mock.generate(mock_tag or "LTX", user_prompt, config.OLLAMA_MODEL)
        prompt = (
            system.strip()
            + "\n\nUSER INPUT:\n"
            + user_prompt.strip()
            + "\n\nGROUNDING (anti-hallucination): stay STRICTLY faithful to "
            "the input above. Invent NO character, object, place or event that "
            "is absent or not clearly implied. Motion, camera angles and "
            "dialogue must follow from the input, not be invented. If the input "
            "is short, stay concise and concrete rather than padding it."
            "\n\nOUTPUT ONLY THE REQUESTED RESULT. No preamble, no repetition."
        )
        payload = {
            "model": model or config.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            # keep_alive override (e.g. 0 to unload immediately in light mode)
            "keep_alive": OLLAMA_KEEP_ALIVE if keep_alive is None else keep_alive,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                # Anti-repetition: a small model (4B) with a verbose persona
                # loops (repeating names/phrases) without these guardrails.
                "repeat_penalty": repeat_penalty,
                "repeat_last_n": 256,
                "top_p": 0.9,
                "top_k": 40,
            },
        }
        # Reasoning models (qwen3, deepseek-r1, gpt-oss…) put their answer in
        # `thinking` and leave `response` empty unless this is off, which read
        # here as "empty Ollama response" and a 502 on every rewrite route.
        # Ollama ignores the flag on models that do not reason.
        payload["think"] = False
        # Ollama structured outputs: a JSON schema in `format` forces the exact
        # shape (reliable). Plain "json" only guarantees valid-but-arbitrary JSON.
        if json_schema is not None:
            payload["format"] = json_schema
        elif json_output:
            payload["format"] = "json"

        url = f"{OLLAMA_BASE_URL}/api/generate"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as gr:
                data = json.loads(gr.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            kind = type(exc).__name__
            raise RuntimeError(f"{kind}: {exc or getattr(exc, 'reason', '')}") from exc

        elapsed_sec = time.monotonic() - t0
        text = (data.get("response") or "").strip()
        if not text:
            if (data.get("thinking") or "").strip():
                raise RuntimeError(
                    f"{model} answered only inside its reasoning block — it may not "
                    "honour think=false; pick a non-reasoning model."
                )
            raise RuntimeError(f"empty Ollama response: {str(data)[:300]}")
        total_duration = data.get("total_duration")
        eval_count = data.get("eval_count") or 0
        eval_duration = data.get("eval_duration") or 0
        meta = {
            "elapsed_sec": round(elapsed_sec, 3),
            "duration_sec": round((total_duration or 0) / 1_000_000_000, 3) if total_duration else round(elapsed_sec, 3),
            "eval_count": eval_count,
            "prompt_eval_count": data.get("prompt_eval_count") or 0,
            "tokens_per_sec": round(eval_count / (eval_duration / 1_000_000_000), 2) if eval_count and eval_duration else None,
        }
        return text, meta
    def _serve_storyboard(self) -> None:
        """Generate connected i2v prompts + FR voice-over for an ordered panel list."""
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req = {}

        def _reply(status: int, payload: dict) -> None:
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

        story = (req.get("story") or "").strip()
        panels = req.get("panels") or []
        if not isinstance(panels, list) or not panels:
            _reply(400, {"error": "no panels"}); return

        lines = [f"STORY: {story or '(free)'}", "PANELS:"]
        for i, p in enumerate(panels, 1):
            idea = (p.get("idea") if isinstance(p, dict) else str(p)) or "(free)"
            lines.append(f"{i}. {idea}")
        user_msg = "\n".join(lines) + (
            '\n\nReturn ONLY valid JSON. Preferred shape: '
            '{"panels":[{"prompt":"...","voiceover":"..."}]}. '
            'No markdown, no code fence.'
        )
        try:
            text, meta = self._ollama_generate(
                PROMPTS.prompt("storyboard"),
                user_msg,
                max_tokens=1800,
                temperature=0.4,
                json_schema=STORYBOARD_SCHEMA,
            )
            parsed = json.loads(text)
            out = _extract_panels(parsed)
            if not isinstance(out, list):
                raise ValueError(f"forme inattendue: {str(parsed)[:200]}")
        except (ValueError, json.JSONDecodeError) as exc:
            _reply(502, {"error": f"invalid storyboard response: {exc}"}); return
        except RuntimeError as exc:
            _reply(502, {"error": str(exc)}); return

        # Match the length to the number of panels.
        result = []
        for i in range(len(panels)):
            item = out[i] if i < len(out) and isinstance(out[i], dict) else {}
            result.append({
                "prompt": (item.get("prompt") or "").strip(),
                "voiceover": (item.get("voiceover") or "").strip(),
            })
        logger.info("STORYBOARD %d panneaux via %s en %.3fs", len(result), config.OLLAMA_MODEL, meta["duration_sec"])
        _reply(200, {"panels": result, "model": config.OLLAMA_MODEL, **meta})
    def _serve_llm(self, transform: personas.Transform) -> None:
        """Rewrite the posted prompt through the local Ollama using `transform`."""
        system, tag = transform.system, transform.tag
        body_len = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(body_len) if body_len else b""
        try:
            req = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            req = {}
        user_prompt = (req.get("prompt") or "").strip()
        lite = bool(req.get("lite"))  # light model while a render is running

        # i2v/keyframe: the first frame is the supplied image -> motion-only
        # prompt, to avoid fractal artifacts (the model must not re-describe it).
        mode = (req.get("mode") or "").strip().lower()
        if tag == "LTX" and mode in ("i2v", "keyframe"):
            system = PROMPTS.prompt("ltx_i2v")
            tag = "LTX-i2v"

        def _reply(status: int, payload: dict) -> None:
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

        if not user_prompt:
            _reply(400, {"error": "prompt vide"}); return
        try:
            text, meta = self._ollama_generate(
                system,
                user_prompt,
                mock_tag=tag,
                max_tokens=transform.max_tokens,
                temperature=transform.temperature,
                model=(OLLAMA_MODEL_LITE if lite else None),
                keep_alive=(0 if lite else None),
            )
            text = transform.postprocess(text)
        except RuntimeError as exc:
            logger.error("Ollama call failed: %s", exc)
            _reply(502, {"error": str(exc)}); return

        logger.info("%s %d→%d chars via %s en %.3fs", tag, len(user_prompt), len(text), config.OLLAMA_MODEL, meta["duration_sec"])
        _reply(200, {"prompt": text, "model": (OLLAMA_MODEL_LITE if lite else config.OLLAMA_MODEL), **meta})

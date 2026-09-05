"""Reasoning models answer in a different field.

Found by running the real chain rather than the recording: qwen3 replies with
`response: ""` and the whole answer in `thinking`, so the proxy raised "empty
Ollama response" and returned 502 for every rewrite route. Ollama takes
`think: false` to turn that off; the payload never sent it.
"""

from __future__ import annotations

import json

from dynamic_video_generator.backends import ollama


def test_the_request_turns_thinking_off():
    """A reasoning model must be asked for the answer, not for its scratchpad."""
    sent: dict = {}

    class _Resp:
        def read(self):
            return json.dumps({"response": "A lighthouse keeper.", "eval_count": 4}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen(req, timeout=None):
        sent.update(json.loads(req.data))
        return _Resp()

    original = ollama.urllib.request.urlopen
    ollama.urllib.request.urlopen = _urlopen
    try:
        text, _meta = ollama.OllamaMixin._ollama_generate(
            object.__new__(ollama.OllamaMixin), "be brief", "a keeper"
        )
    finally:
        ollama.urllib.request.urlopen = original

    assert sent.get("think") is False, f"think not disabled: {sorted(sent)}"
    assert text == "A lighthouse keeper."

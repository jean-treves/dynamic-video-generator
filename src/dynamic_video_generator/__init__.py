"""Fully local multimodal generation gateway.

A same-origin proxy in front of a video backend (LTX-Video), an image
generator (Draw Things) and a local LLM (Ollama), with style packs loaded
at runtime and a replay mode that keeps the interface usable without a
render machine.
"""

__version__ = "0.1.0"

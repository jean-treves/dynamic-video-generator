"""A pack named only in ``.env`` has to actually load.

``.env.example`` ships ``PHOS_PACK=example``, so this is the documented first
run. It did not work: ``config.py`` imports ``personas`` at line 16 and calls
``_load_env()`` at line 85, and ``personas`` read ``$PHOS_PACK`` in its module
body -- 69 lines before the ``.env`` reached ``os.environ``. Every fresh clone
following the README got the bare engine and no explanation.

The fix reads the environment when a pack is loaded rather than when the module
is imported, so no import order can bring the bug back.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Import personas *before* config, which is the order that used to fail.
_PROBE = textwrap.dedent(
    """
    from dynamic_video_generator import personas
    from dynamic_video_generator import config
    print(config.PROMPTS.pack_id, personas.load().pack_id, sep="|")
    """
)


def _run(tmp_path: Path, env_body: str) -> str:
    """Import the package in a fresh interpreter whose cwd holds ``.env``."""
    (tmp_path / ".env").write_text(env_body, encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),  # keep ~/.env and ~/Desktop/.env out of the way
        "PYTHONPATH": str(ROOT / "src"),
    }
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return out.stdout.strip()


def test_a_pack_named_only_in_the_env_file_is_loaded(tmp_path: Path) -> None:
    """PHOS_PACK in .env must reach the library, whatever the import order."""
    assert _run(tmp_path, "PHOS_PACK=example\n") == "example|example"


def test_no_pack_in_the_env_file_still_means_bare_engine(tmp_path: Path) -> None:
    """The empty default has to keep working: absent is not 'example'."""
    assert _run(tmp_path, "OLLAMA_MODEL=gemma3:4b\n") == "|"

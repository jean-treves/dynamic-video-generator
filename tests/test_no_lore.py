"""Guard: the repository stays free of private universe content and hardcoded infra.

The engine is generic; every universe-specific asset (characters, metric axes,
word banks, persona prompts) lives in a pack outside the repository. This test
fails if a private term or an infrastructure identifier comes back into the
code — the easiest regression to reintroduce by copy-pasting from the render
machine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Private vocabulary that must never reappear in the repository.
FORBIDDEN = re.compile(
    r"trinavers|trinator|trinate|kumangyo|grugnamaniq|grugnamniq|epschtein"
    r"|moellis|popeye|blutrinator|trinatosorus|schrimoni|krink|iasaque"
    r"|quadrinator|trnvrs|umpeich|heisenber",
    re.IGNORECASE,
)

#: *Personal* infrastructure identifiers.
#:
#: We target the named subdomain (``my-tunnel.trycloudflare.com``) and the user
#: path (``/Users/someone/``), not the provider domain on its own: the code
#: legitimately needs to test ``hostname.endsWith('.trycloudflare.com')`` to
#: know whether it is being served through a tunnel.
INFRA = re.compile(
    r"[a-z0-9][a-z0-9-]*\.(?:trycloudflare\.com|firebaseio\.com|ngrok-free\.[a-z]+)"
    r"|/Users/[A-Za-z0-9_-]+/",
    re.IGNORECASE,
)

SCANNED_SUFFIXES = {".py", ".html", ".sh", ".md", ".json", ".ts", ".tsx", ".css", ".toml"}
SKIPPED_DIRS = {"node_modules", ".git", ".venv", "__pycache__", ".next", "packs"}

#: This file holds the patterns themselves: it cannot scan itself.
SELF = Path(__file__).resolve()


def _files() -> list[Path]:
    out = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if path.resolve() == SELF:
            continue
        if SKIPPED_DIRS & set(path.relative_to(REPO).parts):
            continue
        out.append(path)
    return out


def test_some_files_are_actually_scanned() -> None:
    """Guard for the guard: too broad a filter would make the test vacuous."""
    assert len(_files()) > 15


@pytest.mark.parametrize("pattern,label", [(FORBIDDEN, "lore"), (INFRA, "infra")])
def test_repository_is_clean(pattern: re.Pattern, label: str) -> None:
    hits = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            hits.append(f"{path.relative_to(REPO)}:{line}: {match.group(0)}")
    assert not hits, f"{label} found in the repository:\n" + "\n".join(hits[:20])


def test_infra_pattern_distinguishes_named_hosts_from_provider_domains() -> None:
    """The pattern must catch a named tunnel without punishing a suffix test."""
    assert INFRA.search("https://mon-tunnel.trycloudflare.com")
    assert INFRA.search("/Users/someone/project")
    assert not INFRA.search("hostname.endsWith('.trycloudflare.com')")
    assert not INFRA.search("/Users/.../ref.png")

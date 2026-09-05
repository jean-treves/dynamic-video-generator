"""Prompt and style-pack loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dynamic_video_generator import personas

REPO = Path(__file__).resolve().parents[1]
EXAMPLE_PACKS = REPO / "packs"


def test_engine_alone_exposes_only_builtin_routes() -> None:
    """With no pack, only the engine's generic routes answer."""
    lib = personas.load(pack_id="", packs_dir=EXAMPLE_PACKS)
    routes = lib.routes()
    assert "/ltx" in routes
    assert "/ground" in routes
    assert "/amplify" in routes
    assert lib.pack_name == ""
    assert all(lib.transforms[r].builtin for r in routes)


def test_engine_prompts_are_non_empty() -> None:
    """Every engine route carries a system prompt loaded from prompts/."""
    lib = personas.load(pack_id="", packs_dir=EXAMPLE_PACKS)
    for route, transform in lib.transforms.items():
        assert transform.system.strip(), f"empty prompt for {route}"


def test_pack_adds_routes_without_touching_builtins() -> None:
    """A pack adds its routes and leaves the engine's in place."""
    bare = personas.load(pack_id="", packs_dir=EXAMPLE_PACKS)
    lib = personas.load(pack_id="example", packs_dir=EXAMPLE_PACKS)
    assert lib.pack_name == "Example"
    assert set(bare.routes()) < set(lib.routes())
    assert "/noir" in lib.routes()
    assert lib.transform("/noir").builtin is False
    assert lib.transform("/ltx").builtin is True


def test_missing_pack_falls_back_to_bare_engine() -> None:
    """A missing pack breaks nothing: we fall back to the bare engine."""
    lib = personas.load(pack_id="pack-that-does-not-exist", packs_dir=EXAMPLE_PACKS)
    assert lib.pack_name == ""
    assert "/ltx" in lib.routes()


def test_roster_placeholder_is_empty_without_pack() -> None:
    """`{roster}` disappears when no pack supplies characters."""
    lib = personas.load(pack_id="", packs_dir=EXAMPLE_PACKS)
    caption = lib.prompt("caption_video")
    assert caption, "the captioning prompt must exist"
    assert "{roster}" not in caption


def test_roster_placeholder_filled_by_pack(tmp_path: Path) -> None:
    """A pack's roster is injected in place of `{roster}`."""
    pack = tmp_path / "p"
    (pack / "prompts").mkdir(parents=True)
    (pack / "roster.txt").write_text(" ROSTER-XYZ.", encoding="utf-8")
    (pack / "pack.json").write_text(
        json.dumps({"id": "p", "name": "P", "roster": "roster.txt"}), encoding="utf-8"
    )
    lib = personas.load(pack_id="p", packs_dir=tmp_path)
    assert "ROSTER-XYZ." in lib.prompt("caption_video")


def test_transform_settings_come_from_the_pack() -> None:
    """max_tokens / temperature declared in pack.json are honoured."""
    lib = personas.load(pack_id="example", packs_dir=EXAMPLE_PACKS)
    runes = lib.transform("/runes")
    assert runes.max_tokens == 200
    assert runes.temperature == pytest.approx(0.3)


def test_dialogue_language_is_a_setting_not_a_constant(monkeypatch):
    """The engine presupposes no spoken language; {lang} carries the choice."""
    from dynamic_video_generator import config, personas

    p = personas.load("")
    monkeypatch.setattr(config, "DIALOGUE_LANG", "Portuguese")
    p._prompts["dialogue"] = 'lines MUST be in {lang}.'
    assert p.prompt("dialogue") == "lines MUST be in Portuguese."

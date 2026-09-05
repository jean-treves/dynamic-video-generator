"""Cipher post-processing (character and phrase substitution)."""

from __future__ import annotations

from pathlib import Path

from dynamic_video_generator import personas

REPO = Path(__file__).resolve().parents[1]


def test_chars_are_transliterated() -> None:
    """Every known character is replaced; unknown ones pass through."""
    cipher = personas.Cipher(chars={"a": "α", "b": "β"})
    assert cipher.apply("abc") == "αβc"


def test_unknown_text_is_unchanged() -> None:
    """With no table, the text comes back unchanged."""
    assert personas.Cipher().apply("hello") == "hello"


def test_phrases_win_over_chars_and_are_not_re_transliterated() -> None:
    """A key phrase becomes its glyph without being transliterated again.

    This is the trap in the cipher: substituting the phrase before the
    character pass without protecting it means its glyph gets transliterated
    in turn, and the result is wrong.
    """
    cipher = personas.Cipher(chars={"a": "α", "b": "β"}, phrases={"ab": "✶"})
    assert cipher.apply("ab") == "✶"


def test_phrase_match_is_case_insensitive() -> None:
    cipher = personas.Cipher(chars={}, phrases={"beyond time": "✶"})
    assert cipher.apply("Beyond Time") == "✶"


def test_example_pack_cipher_runs_end_to_end() -> None:
    """The shipped pack does apply its cipher through the transform."""
    lib = personas.load(pack_id="example", packs_dir=REPO / "packs")
    runes = lib.transform("/runes")
    assert runes.cipher is not None
    out = runes.postprocess("the keeper waits beyond time")
    assert out != "the keeper waits beyond time"
    assert "✶" in out


def test_builtin_transform_postprocess_is_identity() -> None:
    """With no cipher declared, post-processing changes nothing."""
    lib = personas.load(pack_id="", packs_dir=REPO / "packs")
    assert lib.transform("/ltx").postprocess("texte") == "texte"

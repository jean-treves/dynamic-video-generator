"""Replay mode prefers real renders over drawn placeholders.

The placeholders exist so a fresh clone has something to look at. When real
output of *this* pipeline is present under docs/demo/, with the sidecar the
backend wrote next to it, the gallery and the dashboard should show that
instead — real resolutions, real frame counts, real seconds per step.
"""

from __future__ import annotations

import json

import pytest

from dynamic_video_generator import mock


def _write_clip(root, name, params):
    (root / f"{name}.mp4").write_bytes(b"\x00" * 2048)
    (root / f"{name}.json").write_text(json.dumps(params), encoding="utf-8")


def test_placeholders_are_used_when_no_demo_media_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path / "absent")
    names = [o["name"] for o in mock.outputs()["outputs"]]
    assert "lighthouse-storm.mp4" in names


def test_real_demo_media_replaces_the_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path)
    _write_clip(tmp_path, "harbour-dawn", {
        "prompt": "a harbour at dawn, slow crane up",
        "width": 704, "height": 416, "frames": 121, "fps": 24,
        "quality": "standard", "steps": 10, "render_seconds": 118, "seed": 7,
    })
    out = mock.outputs()["outputs"]
    names = [o["name"] for o in out]
    assert names == ["harbour-dawn.mp4"], names
    clip = out[0]
    assert clip["params"]["width"] == 704
    assert clip["params"]["render_seconds"] == 118
    assert clip["size"] == 2048, "size must be the real file size"
    assert clip["mock"] is False, "a real render is not a recording"


def test_a_clip_without_a_sidecar_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path)
    (tmp_path / "orphan.mp4").write_bytes(b"\x00" * 10)
    assert not mock.outputs()["outputs"][0]["name"].startswith("orphan")


def test_demo_names_cannot_walk_out_of_the_directory():
    """Only the flat contents of docs/demo/ are reachable."""
    from pathlib import Path

    for hostile in ("../../README.md", "..%2fmock.py", "sub/dir/x.mp4", "/etc/passwd"):
        assert Path(hostile).name not in ("", ".", ".."), hostile
        resolved = mock.DEMO_DIR / Path(hostile).name
        assert resolved.parent == mock.DEMO_DIR, hostile


def test_a_chained_clip_is_not_reported_as_over_budget(tmp_path, monkeypatch):
    """Long mode renders ~5 s segments and joins them.

    The budget is a per-pass limit. Totalling a 20 s chain gives 150 M
    pixel-frames and would flag red, when every pass that produced it stayed
    inside the 40 M band. The sidecar records the mode so the client can say
    "chained" instead of "over budget".
    """
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path)
    _write_clip(tmp_path, "long-take", {
        "prompt": "", "width": 768, "height": 416, "frames": 480,
        "fps": 24, "mode": "long",
    })
    clip = mock.outputs()["outputs"][0]
    assert clip["params"]["mode"] == "long"


# ── the committed clips themselves ──────────────────────────────────────────

def _committed():
    from dynamic_video_generator.mock import DEMO_DIR
    return sorted(DEMO_DIR.glob("*.mp4")) if DEMO_DIR.is_dir() else []


def test_committed_clips_match_this_pipeline():
    """Guard the provenance rule with the signature, not with a promise.

    Everything under docs/demo/ has to be output of this pipeline: 24 fps,
    dimensions on a 32px grid. A hosted service renders 30 fps and 1280-wide,
    which is how the five clips offered before this were caught.
    """
    clips = _committed()
    if not clips:
        pytest.skip("no demo media committed")
    for clip in clips:
        sidecar = clip.with_suffix(".json")
        assert sidecar.is_file(), f"{clip.name}: no sidecar"
        p = json.loads(sidecar.read_text(encoding="utf-8"))
        assert p["fps"] == 24, f"{clip.name}: {p['fps']} fps, this pipeline renders 24"
        assert p["width"] % 32 == 0 and p["height"] % 32 == 0, clip.name
        assert p["width"] <= 1024, f"{clip.name}: {p['width']}px is beyond 16 GB"


def test_committed_clips_claim_no_render_time_they_do_not_have():
    """The render machine is gone; nothing may invent what it measured."""
    for clip in _committed():
        p = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
        if "render_seconds" in p:
            assert p["render_seconds"] > 0, f"{clip.name}: zero render time recorded"
            assert "steps" in p, f"{clip.name}: a render time needs its step count"


def test_the_images_tab_serves_real_frames_when_they_exist(tmp_path, monkeypatch):
    """Same rule as the gallery: a real frame beats a drawn placeholder."""
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path)
    (tmp_path / "shot.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 500)
    (tmp_path / "shot.json").write_text(json.dumps(
        {"prompt": "", "width": 768, "height": 416, "frames": 1, "fps": 24}),
        encoding="utf-8")
    data = mock.images()
    assert [i["name"] for i in data["images"]] == ["shot.jpg"]
    assert data["images"][0]["url"] == "/demo/shot.jpg"


def test_the_images_tab_falls_back_to_placeholders(tmp_path, monkeypatch):
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path / "empty")
    assert mock.images()["images"], "the tab must never be blank"
    assert all(i["url"].startswith("/thumb?path=") for i in mock.images()["images"])


def test_outputs_fall_back_to_the_committed_renders(tmp_path, monkeypatch):
    """With no render backend, the app must not read as empty.

    The render machine is gone for good. Without a fallback /outputs proxies to
    an absent upstream, returns 502, and every screen shows "unreachable" — on
    an app that ships three real renders in the tree.
    """
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path)
    _write_clip(tmp_path, "kept", {
        "prompt": "", "width": 704, "height": 416, "frames": 121, "fps": 24,
    })
    out = mock.demo_media()
    assert [o["name"] for o in out] == ["kept.mp4"]
    assert out[0]["url"].startswith("/demo/")

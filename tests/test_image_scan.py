"""The image scanner must never leave the render backend's own output.

Found by inspecting what /img-list actually returned on this machine: Draw
Things' sandbox container exposes Data/Pictures, the scanner walked it with
rglob("*"), and the walk descended into `Photothèque.photoslibrary`. The tab
was serving the user's macOS Photos library over HTTP — and over the public
Cloudflare tunnel whenever one was up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dynamic_video_generator.backends import drawthings

HOME = Path.home()
CONTAINER = HOME / "Library/Containers/com.liuliu.draw-things/Data/Pictures"


@pytest.mark.parametrize("rel", [
    "Photothèque.photoslibrary/resources/derivatives/cvt/6/ABC_cvt_t0018.jpeg",
    "Photos Library.photoslibrary/originals/0/IMG_0001.jpeg",
    "Old.aplibrary/Masters/2019/shot.jpg",
    "Movie.fcpbundle/Render Files/frame.png",
    "Some.app/Contents/Resources/icon.png",
    ".thumbnails/cache.png",
])
def test_personal_and_package_stores_are_refused(rel):
    assert not drawthings.is_scannable(CONTAINER / rel), rel


@pytest.mark.parametrize("rel", [
    "Generated/2026-08-01-portrait.png",
    "Assets/render_0042.jpg",
])
def test_render_output_is_still_scanned(rel):
    assert drawthings.is_scannable(CONTAINER / rel, CONTAINER), rel


def test_a_bare_media_folder_is_not_render_output():
    """A file sitting directly in Pictures is the user's, not the backend's.

    The old contract accepted it; the sandbox maps that folder onto ~/Pictures.
    """
    assert not drawthings.is_scannable(CONTAINER / "img_20260830_seed1234.webp", CONTAINER)


def test_scan_depth_is_bounded():
    """A deep tree below an output directory must not be walked indefinitely."""
    deep = CONTAINER / "Generated" / "a/b/c/d/e" / "x.png"
    assert not drawthings.is_scannable(deep, CONTAINER)


def test_mock_mode_lists_images_without_walking_home(tmp_path, monkeypatch):
    """Replay mode answers /img-list from what it ships, never from home."""
    from dynamic_video_generator import mock

    called = []
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path / "empty")
    monkeypatch.setattr(Path, "rglob", lambda self, pat: called.append(self) or [])

    data = mock.images()
    assert data["mock"] is True
    assert data["images"], "replay mode should still populate the Images tab"
    assert not called, "replay mode walked the filesystem"

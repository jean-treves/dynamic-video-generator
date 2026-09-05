"""The image scanner may only read what the render backend wrote.

Draw Things runs sandboxed, and its container maps Data/Pictures onto the
user's own ~/Pictures — Photo Booth, the Photos library, personal albums. A
denylist of package extensions was not enough: "Bibliothèque Photo Booth" is
an ordinary directory, so it passed, and the Images tab served personal photos
again. The scanner now works from an allowlist instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dynamic_video_generator.backends import drawthings

HOME = Path.home()
CONTAINER = HOME / "Library/Containers/com.liuliu.draw-things/Data"


@pytest.mark.parametrize("rel", [
    "Pictures/Bibliothèque Photo Booth/Pictures/Photo le 26-08-2026 à 19.34.jpg",
    "Pictures/Photothèque.photoslibrary/resources/derivatives/cvt/6/X_cvt.jpeg",
    "Pictures/Photos-HEIC/IMG_0001.jpg",
    "Pictures/Floral Shoppe/cover.png",
    "Documents/Downloads/whatever.png",
])
def test_personal_media_is_never_reachable(rel):
    """Anything that is not a render-output directory stays unread."""
    assert not drawthings.is_scannable(CONTAINER / rel, CONTAINER), rel


@pytest.mark.parametrize("rel", [
    "Documents/Generated/2026-08-01-portrait.png",
    "Documents/Assets/render_0042.jpg",
    "Pictures/Generated/img_20260830_seed1234.webp",
])
def test_render_output_directories_are_reachable(rel):
    assert drawthings.is_scannable(CONTAINER / rel, CONTAINER), rel


def test_an_explicit_root_opts_a_directory_in(monkeypatch):
    """A folder someone deliberately names is theirs to expose."""
    monkeypatch.setattr(drawthings, "EXTRA_IMAGE_ROOTS", (CONTAINER / "Pictures/Nanimoor",))
    assert drawthings.is_scannable(CONTAINER / "Pictures/Nanimoor/a.png", CONTAINER)

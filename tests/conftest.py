"""Shared fixtures."""

from __future__ import annotations

import pytest

from dynamic_video_generator import mock


@pytest.fixture
def no_demo_media(tmp_path, monkeypatch):
    """Point DEMO_DIR at an empty directory.

    Tests about the recorded fallback must keep testing the recording, not
    whatever real renders happen to be committed under docs/demo/.
    """
    monkeypatch.setattr(mock, "DEMO_DIR", tmp_path / "empty")
    return tmp_path

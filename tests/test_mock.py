"""Replay mode: the interface must stay usable with no backend and no LLM."""

from __future__ import annotations

from dynamic_video_generator import mock


def test_generate_is_deterministic_and_quotes_the_prompt() -> None:
    """The recorded response echoes the subject, and two calls agree."""
    first, meta = mock.generate("LTX", "a lighthouse in a storm", "gemma3:4b")
    second, _ = mock.generate("LTX", "a lighthouse in a storm", "gemma3:4b")
    assert first == second
    assert "a lighthouse in a storm" in first
    assert meta["mock"] is True


def test_every_tag_has_a_usable_answer() -> None:
    """An unknown tag falls back to the default template, never to nothing."""
    for tag in ("LTX", "LTX-i2v", "GROUND", "AMPLIFY", "TAG-INCONNU"):
        text, _ = mock.generate(tag, "a subject", "m")
        assert len(text) > 40


def test_outputs_are_shaped_like_the_real_backend(no_demo_media) -> None:
    """Mocked outputs carry the fields the interface reads."""
    data = mock.outputs()
    assert data["mock"] is True
    items = data["outputs"]
    assert len(items) >= 8
    for item in items:
        assert {"name", "path", "kind", "prompt", "mtime_ts"} <= set(item)
        assert item["kind"] in ("video", "image")
    assert any(i["kind"] == "video" for i in items)
    assert any(i["kind"] == "image" for i in items)


def test_outputs_are_spread_over_time(no_demo_media) -> None:
    """Timestamps spread out, otherwise the timeline and 7-day filter are empty."""
    items = mock.outputs()["outputs"]
    stamps = [i["mtime_ts"] for i in items]
    assert len(set(stamps)) == len(stamps)
    assert max(stamps) - min(stamps) > 7 * 86_400 - 1


def test_upstream_knows_render_routes(no_demo_media) -> None:
    assert mock.upstream("/generate", {})[0] == 200
    assert mock.upstream("/status")[1]["status"] == "done"
    assert mock.upstream("/outputs")[1]["outputs"]


def test_upstream_returns_none_for_unknown_routes() -> None:
    """Unknown route -> None, so the caller answers 503 and not a fake success."""
    assert mock.upstream("/quelque-chose-dinconnu") is None


def test_storyboard_returns_requested_panel_count() -> None:
    panels = mock.storyboard(3)
    assert len(panels) == 3
    for panel in panels:
        assert panel["prompt"] and panel["voiceover"]


def test_outputs_carry_generation_parameters(no_demo_media) -> None:
    """The dashboard derives every figure from these — they must be present."""
    for item in mock.outputs()["outputs"]:
        p = item["params"]
        assert {"width", "height", "frames", "fps", "steps", "render_seconds"} <= set(p)
        assert p["width"] % 32 == 0 and p["height"] % 32 == 0
        if item["kind"] == "video":
            assert p["frames"] % 8 == 1, f"{item['name']}: frames % 8 must be 1"


def test_render_times_follow_the_measured_curve(no_demo_media) -> None:
    """Seconds per step must stay plausible: cheap inside the budget, and an
    order of magnitude worse once the clip is well over it."""
    inside, over = [], []
    for item in mock.outputs()["outputs"]:
        if item["kind"] != "video":
            continue
        p = item["params"]
        px = p["width"] * p["height"] * p["frames"] / 1e6
        (inside if px <= 40 else over).append(p["render_seconds"] / p["steps"])
    assert inside and over, "the sample must cover both regimes"
    assert max(inside) < 20, "inside the budget a step should stay ~12 s"
    assert max(over) > 50, "past the budget a step should collapse"


def test_sidecar_returns_the_parameters_of_a_known_output() -> None:
    data = mock.sidecar("/outputs/empty-hotel.mp4")
    assert data["params"]["frames"] == 169
    assert mock.sidecar("/outputs/does-not-exist.mp4")["params"] == {}


def test_poster_is_svg_and_carries_the_render_spec():
    """The placeholder must name what it stands for, and stay labelled as mock."""
    svg = mock.poster("/outputs/empty-hotel.mp4").decode()
    assert svg.startswith("<svg")
    assert "1024x576" in svg and "169f" in svg
    assert "MOCK" in svg


def test_poster_survives_an_unknown_path():
    svg = mock.poster("/outputs/never-rendered.mp4").decode()
    assert svg.startswith("<svg") and "no sidecar" in svg

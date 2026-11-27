"""Unit tests for the deterministic parts of the analysis pipeline.

Nothing here is stubbed: scoring runs against the real `config/scoring.yaml`, and the visual tests
decode real FFmpeg-generated fixtures. The assertions are about *documented behaviour*, not about
whatever the code happens to produce — several of them exist because the naive implementation got
them wrong first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.scoring import (
    CRITERIA,
    SegmentView,
    build_windows,
    categorise,
    deduplicate,
    extract_features,
    iou,
    load_scoring_config,
    score_moment,
)
from app.services.visual import analyse_frames, detect_scenes, sample_frames

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "generated"
CONFIG = Path(__file__).resolve().parents[3] / "config" / "scoring.yaml"


def fixture(name: str) -> Path:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"media fixture missing: {name} (run ops/scripts/make_fixtures.sh)")
    return path


@pytest.fixture(scope="module")
def config():
    return load_scoring_config(CONFIG)


# --------------------------------------------------------------------- configuration


def test_weights_sum_to_one(config):
    assert sum(config.weights.values()) == pytest.approx(1.0)
    assert set(config.weights) == set(CRITERIA)
    assert config.version


def test_weights_that_do_not_sum_to_one_are_rejected(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(CONFIG.read_text().replace("weight: 0.15", "weight: 0.50", 1))
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        load_scoring_config(bad)


def test_missing_criterion_is_rejected(tmp_path: Path):
    """Removing a criterion's weight block must fail loudly, not silently rescale."""
    text = CONFIG.read_text()
    marker = "  clarity:\n"
    start = text.index(marker)
    # Find the next top-level (2-space) key after `clarity:` to bound the removal.
    end = text.index("\n# How the ten criteria", start)
    bad_text = text[:start] + text[end + 1 :]
    bad = tmp_path / "missing.yaml"
    bad.write_text(bad_text)
    with pytest.raises(ValueError, match=r"clarity|sum to 1\.0"):
        load_scoring_config(bad)


# --------------------------------------------------------------------- features


def test_features_detect_a_question_opening():
    f = extract_features("Why do most people fail at this?", 5000)
    assert f.opens_with_question is True
    assert f.word_count == 7


def test_features_measure_filler_and_diversity():
    clean = extract_features("Deleting the file fixed the crash.", 4000)
    messy = extract_features("Um, like, you know, um, it was, like, kind of broken.", 4000)
    assert messy.filler_ratio > clean.filler_ratio
    assert clean.lexical_diversity > 0


def test_features_flag_context_dependent_references():
    dependent = extract_features("He said that this was the thing they meant earlier.", 4000)
    standalone = extract_features("Deleting the cache fixed the crash immediately.", 4000)
    assert dependent.dependent_refs > standalone.dependent_refs


# --------------------------------------------------------------------- scoring


def test_scores_are_bounded_and_complete(config):
    result = score_moment(
        text="Most people think learning to code is about memorising syntax. It is not.",
        duration_ms=9000,
        config=config,
    )
    assert set(result.criteria) == set(CRITERIA)
    assert all(0 <= v <= 100 for v in result.criteria.values())
    assert 0 <= result.overall <= 100
    assert set(result.rollup) == {
        "content_potential",
        "technical_quality",
        "context_completeness",
        "audience_relevance",
    }
    assert all(0 <= v <= 100 for v in result.rollup.values())


def test_overall_is_the_weighted_sum(config):
    result = score_moment(text="A short but complete thought.", duration_ms=6000, config=config)
    expected = sum(result.criteria[c] * config.weights[c] for c in CRITERIA)
    assert result.overall == pytest.approx(expected, abs=0.01)


def test_scoring_is_deterministic(config):
    text = "The real skill is breaking a problem into pieces small enough to test."
    a = score_moment(text=text, duration_ms=8000, config=config)
    b = score_moment(text=text, duration_ms=8000, config=config)
    assert a.criteria == b.criteria
    assert a.overall == b.overall


def test_measured_visual_quality_is_used_when_supplied(config):
    without = score_moment(text="Some words here.", duration_ms=8000, config=config)
    with_low = score_moment(
        text="Some words here.", duration_ms=8000, config=config, visual_quality=10.0
    )
    assert with_low.criteria["visual_quality"] == 10.0
    # Unmeasured must be explicitly neutral, not silently "good".
    assert without.criteria["visual_quality"] == 50.0
    assert "not measured" in without.evidence["visual_quality"]


def test_heuristic_method_is_disclosed(config):
    result = score_moment(text="Some words.", duration_ms=6000, config=config)
    assert result.method == "heuristic"
    assert any("not view predictions" in note for note in result.notes)


def test_evidence_is_produced_for_every_criterion(config):
    result = score_moment(
        text="Why do most people fail? Because they never finish anything.",
        duration_ms=7000,
        config=config,
    )
    for criterion in CRITERIA:
        assert result.evidence[criterion], f"no evidence recorded for {criterion}"


# --------------------------------------------------------------------- windows


def _segments():
    return [
        SegmentView(0, 0, 4000, "One complete sentence here."),
        SegmentView(1, 4200, 9000, "A second sentence follows it."),
        SegmentView(2, 9200, 15000, "And a third closes the thought."),
        SegmentView(3, 15200, 22000, "A later, unrelated remark appears."),
    ]


def test_windows_respect_the_duration_band(config):
    windows = build_windows(_segments(), target_min_ms=6000, target_max_ms=12000, config=config)
    assert windows
    for window in windows:
        assert window.duration_ms >= 3000  # configured hard floor
        assert window.duration_ms <= 12000  # ceiling respected


def test_windows_are_sentence_aligned(config):
    """A window must start and end on segment boundaries — never mid-sentence."""
    segments = _segments()
    boundaries = {s.start_ms for s in segments} | {s.end_ms for s in segments}
    for window in build_windows(segments, target_min_ms=6000, target_max_ms=12000, config=config):
        assert window.start_ms in boundaries
        assert window.end_ms in boundaries


def test_no_segments_yields_no_windows(config):
    assert build_windows([], target_min_ms=5000, target_max_ms=10000, config=config) == []


def test_iou_maths():
    a = type("W", (), {"start_ms": 0, "end_ms": 10000})()
    b = type("W", (), {"start_ms": 5000, "end_ms": 15000})()
    assert iou(a, b) == pytest.approx(5000 / 15000)
    assert iou(a, a) == pytest.approx(1.0)


def test_deduplicate_suppresses_overlap_and_explains(config):
    windows = build_windows(_segments(), target_min_ms=6000, target_max_ms=12000, config=config)
    scored = [
        (w, score_moment(text=w.text, duration_ms=w.duration_ms, config=config)) for w in windows
    ]
    ranked = deduplicate(scored, max_overlap=0.15, min_gap_ms=3000)
    assert len(ranked) == len(scored), "every candidate must be reported, kept or suppressed"
    kept = [r for r in ranked if r[2] is None]
    assert kept, "at least one candidate must survive"
    # No two kept candidates may overlap beyond the threshold.
    for i, (w1, _, _) in enumerate(kept):
        for w2, _, _ in kept[i + 1 :]:
            assert iou(w1, w2) <= 0.15


def test_categorise_respects_the_enabled_allowlist():
    assert categorise("Why does this happen to everyone?", []) == "important_question"
    assert categorise("Why does this happen?", ["educational"]) == "other"
    assert categorise("Most people believe a myth about this.", []) == "surprising_fact"


# --------------------------------------------------------------------- visual (real frames)


def test_scene_detection_finds_the_cuts_the_fixture_contains():
    """black_frames.mp4 is built as 10 s of test pattern, 2 s black, 8 s pattern.

    The cuts are therefore at exactly 10 000 ms and 12 000 ms. Asserting those numbers is what
    proves the detector reads real pixel data — a version that always returns one scene would
    still "pass" a test that only checked for a non-empty list.
    """
    path = fixture("black_frames.mp4")
    scenes = detect_scenes(path, duration_ms=20000)
    assert len(scenes) == 3, f"expected 3 shots, got {[(s.start_ms, s.end_ms) for s in scenes]}"
    starts = [s.start_ms for s in scenes]
    assert starts[0] == 0
    assert 9_000 <= starts[1] <= 11_000
    assert 11_000 <= starts[2] <= 13_000
    assert scenes[-1].end_ms == 20000


def test_single_shot_video_reports_one_scene():
    scenes = detect_scenes(fixture("silent.mp4"), duration_ms=10000)
    assert len(scenes) == 1


def test_frame_sampling_measures_real_brightness():
    path = fixture("black_frames.mp4")
    frames = sample_frames(path, duration_ms=20000, count=8)
    assert len(frames) == 8
    # The 2-second black section must show up as a genuinely dark frame.
    assert min(f.brightness for f in frames) < 20
    assert max(f.brightness for f in frames) > 100


def test_black_section_lowers_the_quality_score():
    report = analyse_frames(fixture("black_frames.mp4"), duration_ms=20000)
    assert report.metrics["flat_frame_ratio"] > 0
    assert report.metrics["visual_quality_score"] < 80
    assert any("near-black" in note for note in report.notes)


def test_report_metrics_are_real_numbers():
    report = analyse_frames(fixture("silent.mp4"), duration_ms=10000)
    metrics = report.metrics
    assert 0 <= metrics["mean_brightness"] <= 255
    assert metrics["mean_contrast"] > 0
    assert metrics["mean_sharpness"] > 0
    assert metrics["scene_count"] == 1

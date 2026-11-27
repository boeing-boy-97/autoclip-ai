"""Unit tests for caption cue construction and the render plan.

These are the tests that keep the module's central promise: a caption only appears at a moment the
transcription actually recorded. Most of them check that bad input degrades *visibly* rather than
producing a plausible-looking lie.
"""

from __future__ import annotations

import pytest

from app.services.captions import (
    Cue,
    Word,
    build_cues,
    build_track,
    to_ass,
    to_webvtt,
    words_from_segments,
)
from app.services.render import Template, build_plan, nudge_to_word_gap


def w(text: str, start: int, end: int) -> dict:
    return {"w": text, "s": start, "e": end, "p": 0.9}


def seg(idx: int, start: int, end: int, words: list[dict]) -> dict:
    return {"idx": idx, "start_ms": start, "end_ms": end, "words": words}


class TestWordExtraction:
    def test_words_are_flattened_in_time_order(self):
        segments = [
            seg(0, 0, 1000, [w("one", 0, 400), w("two", 400, 900)]),
            seg(1, 1000, 2000, [w("three", 1000, 1400)]),
        ]
        words, missing = words_from_segments(segments)
        assert [x.text for x in words] == ["one", "two", "three"]
        assert missing == 0

    def test_words_without_timing_are_dropped_and_counted(self):
        """A word with no timestamp must not be given an invented one."""
        segments = [
            seg(0, 0, 1000, [w("good", 0, 400), {"w": "orphan"}, {"w": "", "s": 1, "e": 2}]),
            seg(1, 1000, 2000, [{"w": "bad", "s": 1500, "e": 1400}]),  # end <= start
        ]
        words, missing = words_from_segments(segments)
        assert [x.text for x in words] == ["good"]
        assert missing == 2

    def test_null_words_payload_is_tolerated(self):
        words, missing = words_from_segments([seg(0, 0, 100, None)])
        assert words == []
        assert missing == 0


class TestCueBuilding:
    def test_cue_respects_word_budget(self):
        """A 4-word style must produce 4-word cues, not 8 words wrapped to 4 lines."""
        words = [Word(f"w{i}", i * 300, i * 300 + 250) for i in range(12)]
        cues, captured, _ = build_cues(
            words, clip_start_ms=0, clip_end_ms=4000, max_words_per_cue=4
        )
        assert all(c.word_count <= 4 for c in cues), [c.word_count for c in cues]
        assert captured == 12

    def test_cue_breaks_on_a_long_pause(self):
        words = [Word("before", 0, 400), Word("after", 2000, 2400)]
        cues, _, _ = build_cues(words, clip_start_ms=0, clip_end_ms=3000, max_gap_ms=700)
        assert len(cues) == 2, "a 1.6s silence must split the cue"

    def test_short_pause_does_not_split(self):
        words = [Word("a", 0, 300), Word("b", 400, 700)]
        cues, _, _ = build_cues(words, clip_start_ms=0, clip_end_ms=1000, max_gap_ms=700)
        assert len(cues) == 1

    def test_cue_times_are_clip_relative(self):
        """A caption file attached to the clip must start at 0, not at the source timestamp."""
        words = [Word("hello", 10_000, 10_400)]
        cues, _, _ = build_cues(words, clip_start_ms=10_000, clip_end_ms=20_000)
        assert cues[0].start_ms == 0
        assert cues[0].end_ms == 400

    def test_words_outside_the_window_are_excluded(self):
        words = [Word("outside", 0, 300), Word("inside", 5000, 5400)]
        cues, captured, _ = build_cues(words, clip_start_ms=5000, clip_end_ms=6000)
        assert captured == 1
        assert "outside" not in cues[0].text

    def test_partial_word_at_clip_end_is_truncated_and_counted(self):
        words = [Word("crosses", 5500, 6500)]
        cues, _, truncated = build_cues(words, clip_start_ms=5000, clip_end_ms=6000)
        assert truncated == 1
        assert cues[0].end_ms <= 1000, "a cue may never outlive the clip"

    def test_empty_window_yields_no_cues(self):
        words = [Word("elsewhere", 0, 300)]
        cues, captured, _ = build_cues(words, clip_start_ms=5000, clip_end_ms=6000)
        assert cues == []
        assert captured == 0

    def test_zero_length_cue_never_reaches_the_output(self):
        """A renderer cannot display a cue with no duration, so it must not be emitted."""
        # A word that only just touches the clip start produces a zero-length cue unless dropped.
        words = [Word("edge", 4999, 5001)]
        cues, _, _ = build_cues(words, clip_start_ms=5000, clip_end_ms=6000)
        assert all(c.end_ms > c.start_ms for c in cues), cues


class TestTrackCoverage:
    def test_full_coverage_reports_one(self):
        segments = [seg(0, 0, 1000, [w("a", 0, 300), w("b", 300, 600)])]
        track = build_track(segments, clip_start_ms=0, clip_end_ms=1000)
        assert track.coverage == 1.0
        assert track.words_without_timing == 0
        assert track.warnings == []

    def test_missing_timing_is_reported_not_hidden(self):
        segments = [seg(0, 0, 1000, [w("a", 0, 300), {"w": "no_timing"}])]
        track = build_track(segments, clip_start_ms=0, clip_end_ms=1000)
        assert track.words_without_timing == 1
        assert any("not captioned" in x for x in track.warnings)
        assert track.coverage == 1.0  # every word that *had* a timing was captioned

    def test_no_words_produces_a_warning_not_an_exception(self):
        track = build_track([], clip_start_ms=0, clip_end_ms=1000)
        assert track.cues == []
        assert track.has_captions is False

    def test_truncated_words_are_counted(self):
        segments = [seg(0, 0, 5000, [w("crosses", 5500, 6500)])]
        track = build_track(segments, clip_start_ms=5000, clip_end_ms=6000)
        assert track.truncated_at_clip_end == 1


class TestSerialisation:
    def make(self) -> list[Cue]:
        return [Cue(0, 1240, "When I started, I", 4), Cue(1240, 2320, "spent three weeks", 3)]

    def test_webvtt_timestamps(self):
        vtt = to_webvtt(self.make())
        assert vtt.startswith("WEBVTT")
        assert "00:00:00.000 --> 00:00:01.240" in vtt
        assert "When I started, I" in vtt

    def test_ass_has_expected_sections(self):
        ass = to_ass(self.make(), width=1080, height=1920, font_size_px=64)
        assert "[Script Info]" in ass
        assert "[V4+ Styles]" in ass
        assert "[Events]" in ass
        assert "Dialogue: 0,0:00:00.00,0:00:01.24" in ass

    def test_ass_scales_font_into_script_resolution(self):
        """ASS sizes are relative to 384x288; a 64px caption at 1080 wide is 23 in script space."""
        ass = to_ass(self.make(), width=1080, height=1920, font_size_px=64)
        style = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
        assert ",23," in style, style

    def test_ass_never_exceeds_declared_lines(self):
        cues = [Cue(0, 1000, "one two three four five six seven eight", 8)]
        ass = to_ass(cues, width=1080, height=1920, max_lines=2)
        dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
        assert dialogue.count(r"\N") == 1, "8 words should wrap to exactly 2 lines"


class TestTemplate:
    def test_every_shipped_template_loads(self):
        """A template that fails to parse is a broken product, not a user error."""
        for key in [
            "podcast",
            "business",
            "comedy",
            "educational",
            "interview",
            "motivational",
            "news",
            "tutorial",
        ]:
            t = Template.load(key)
            assert t.width == 1080, key
            assert t.height == 1920, key
            assert t.max_words_per_cue >= 1

    def test_unknown_template_raises(self):
        from app.services.render import RenderError

        with pytest.raises(RenderError, match="no template"):
            Template.load("does_not_exist")


class TestWordGapNudging:
    def test_start_inside_a_word_moves_back(self):
        words = [Word("hello", 1000, 1400)]
        start, _end, adj = nudge_to_word_gap(1200, 2000, words)
        assert start == 1000
        assert adj["start_delta_ms"] == -200

    def test_end_inside_a_word_moves_forward(self):
        words = [Word("world", 1800, 2200)]
        _start, end, adj = nudge_to_word_gap(1000, 2000, words)
        assert end == 2200
        assert adj["end_delta_ms"] == 200

    def test_huge_nudge_is_refused_and_explained(self):
        words = [Word("enormous", 0, 5000)]
        _start, end, adj = nudge_to_word_gap(4000, 6000, words, max_nudge_ms=250)
        assert end == 6000, "must not swallow a full second of speech"
        assert any("left as-is" in r for r in adj["reason"])

    def test_clean_boundary_is_untouched(self):
        words = [Word("a", 0, 300), Word("b", 1400, 1800)]
        start, end, adj = nudge_to_word_gap(300, 1400, words)
        assert (start, end) == (300, 1400)
        assert adj["reason"] == []


class TestRenderPlan:
    def test_plan_records_everything_needed_to_explain_the_file(self):
        template = Template.load("podcast")
        plan = build_plan(
            clip_id="abc",
            template=template,
            start_ms=1000,
            end_ms=5000,
            cue_count=4,
            source_width=640,
            source_height=360,
            adjustments={"start_delta_ms": 0, "end_delta_ms": 0, "reason": []},
            warnings=["one warning"],
        )
        d = plan.to_dict()
        assert d["window"] == {"start_ms": 1000, "end_ms": 5000, "duration_ms": 4000}
        assert d["canvas"]["width"] == 1080
        assert d["captions"]["cue_count"] == 4
        assert d["source_dimensions"] == {"width": 640, "height": 360}
        assert d["warnings"] == ["one warning"]

    def test_invalid_window_is_rejected(self):
        from app.services.render import RenderError

        with pytest.raises(RenderError, match="end must be after"):
            build_plan(
                clip_id="abc",
                template=Template.load("podcast"),
                start_ms=5000,
                end_ms=5000,
                cue_count=0,
                source_width=None,
                source_height=None,
            )

    def test_caption_count_of_zero_is_recorded_honestly(self):
        plan = build_plan(
            clip_id="abc",
            template=Template.load("podcast"),
            start_ms=0,
            end_ms=1000,
            cue_count=0,
            source_width=None,
            source_height=None,
        )
        assert plan.has_captions is False
        assert plan.to_dict()["captions"]["burned"] is False


class TestStructuredLogging:
    """`logger.info(extra={...})` silently reserves a set of attribute names.

    Passing a reserved name raises `KeyError` inside `makeRecord`, which is not a logging failure
    but a raised exception in the calling function — so a generate that had already written its rows
    would return 500. `created` was exactly this bug.
    """

    RESERVED = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "asctime",
            "taskName",
        }
    )

    def test_no_log_call_uses_a_reserved_extra_key(self):
        import pathlib
        import re

        pattern = re.compile(r"extra=\{([^}]*)\}", re.S)
        key = re.compile(r'"([A-Za-z_]+)"\s*:')
        offenders: list[str] = []
        app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
        for path in app_dir.rglob("*.py"):
            for match in pattern.finditer(path.read_text()):
                for name in key.findall(match.group(1)):
                    if name in self.RESERVED:
                        offenders.append(f"{path.name}: {name}")
        assert not offenders, f"reserved LogRecord keys used as extra= fields: {offenders}"

    def test_logging_with_the_reported_keys_does_not_raise(self):
        import logging

        record = logging.getLogger("test").makeRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "msg",
            (),
            None,
            None,
            extra={"project_id": "p", "created_count": 1, "existing_count": 0},
            sinfo=None,
        )
        assert record.created_count == 1

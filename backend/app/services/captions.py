"""Caption cues derived from real ASR word timings — never from guesses.

The rule this module exists to enforce: **a caption may only show a word at a time the
transcription actually recorded for it.** Four things follow from that, and each is tested:

1. Cues are built from the word list, not by dividing a segment's text evenly. Even division
   produces cues that drift out of sync within a sentence.
2. A cue may not span a gap where the speaker stopped talking for a noticeable pause — otherwise
   text hangs on screen over silence.
3. A word with no timing is not captioned at a made-up moment. It is reported in `truthful`
   accounting so the caller can see the coverage gap instead of being quietly lied to.
4. A cue never outlives the clip. Anything past the clip end is dropped, and the cue list is
   clipped, not stretched.

Output is WebVTT and ASS. Both are generated from the same cue list, so the two files cannot
disagree about what is on screen when.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# A pause longer than this ends a cue. People read in breaths, not in segments.
DEFAULT_MAX_GAP_MS = 700
# Absolute ceiling on how long a single cue stays on screen, regardless of speech density.
DEFAULT_MAX_CUE_MS = 6000
# Below this a cue is too brief to register; it is merged into its neighbour instead.
DEFAULT_MIN_CUE_MS = 400

_SENTENCE_END = re.compile(r"[.!?]['\"\u2019\u201d]?$")


@dataclass(slots=True)
class Word:
    text: str
    start_ms: int
    end_ms: int


@dataclass(slots=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str
    word_count: int = 0

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(slots=True)
class CaptionTrack:
    cues: list[Cue]
    # Honest accounting, surfaced to the caller so coverage gaps are visible rather than hidden.
    words_in_range: int = 0
    words_captioned: int = 0
    words_without_timing: int = 0
    truncated_at_clip_end: int = 0
    source: str = "asr_word_timings"
    warnings: list[str] = field(default_factory=list)

    @property
    def has_captions(self) -> bool:
        return bool(self.cues)

    @property
    def coverage(self) -> float:
        """Fraction of in-range words that got a real timestamp. 1.0 means full coverage."""
        if self.words_in_range == 0:
            return 0.0
        return self.words_captioned / self.words_in_range


def _clean(text: str) -> str:
    return " ".join(text.split())


def words_from_segments(segments: list[dict]) -> tuple[list[Word], int]:
    """Flatten `transcript_segments.words` payloads into a timed word list.

    Returns `(words, without_timing)`. Words whose stored entry is missing a usable start/end are
    counted and dropped rather than assigned an invented timestamp.
    """
    out: list[Word] = []
    without_timing = 0
    for seg in segments:
        raw = seg.get("words") or []
        if not isinstance(raw, list):
            continue
        for entry in raw:
            if not isinstance(entry, dict):
                without_timing += 1
                continue
            text = _clean(str(entry.get("w") or ""))
            start = entry.get("s")
            end = entry.get("e")
            if not text:
                continue
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                without_timing += 1
                continue
            out.append(Word(text=text, start_ms=start, end_ms=end))
    out.sort(key=lambda w: (w.start_ms, w.end_ms))
    return out, without_timing


def build_cues(
    words: list[Word],
    *,
    clip_start_ms: int,
    clip_end_ms: int,
    max_words_per_cue: int = 4,
    max_gap_ms: int = DEFAULT_MAX_GAP_MS,
    max_cue_ms: int = DEFAULT_MAX_CUE_MS,
    min_cue_ms: int = DEFAULT_MIN_CUE_MS,
) -> tuple[list[Cue], int, int]:
    """Group timed words into cues inside the clip window.

    Returns `(cues, words_captioned, truncated)`. Cue times are **clip-relative** (0 = clip
    start), because that is what a caption file attached to the clip must use.
    """
    # `max_words_per_cue` is already "words visible at once". Multiplying it by max_lines here
    # was wrong: it produced 8-word cues for a 4-word style, which then wrapped to 4 lines and
    # covered the middle of the frame. max_lines constrains wrapping, not the word budget.
    per_cue = max(1, max_words_per_cue)

    in_range = [
        w for w in words if w.end_ms > clip_start_ms and w.start_ms < clip_end_ms
    ]
    truncated = sum(1 for w in in_range if w.end_ms > clip_end_ms)
    if not in_range:
        return [], 0, 0

    cues: list[Cue] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        start = current[0].start_ms
        end = current[-1].end_ms
        cues.append(
            Cue(
                start_ms=max(0, start - clip_start_ms),
                end_ms=min(clip_end_ms, end) - clip_start_ms,
                text=" ".join(w.text for w in current),
                word_count=len(current),
            )
        )
        current.clear()

    for word in in_range:
        if current:
            gap = word.start_ms - current[-1].end_ms
            longest = current[-1].end_ms - current[0].start_ms
            too_long = longest + (word.end_ms - current[-1].end_ms) > max_cue_ms
            sentence_done = bool(_SENTENCE_END.search(current[-1].text))
            if gap > max_gap_ms or len(current) >= per_cue or too_long:
                flush()
            elif sentence_done and len(current) >= max(1, per_cue // 2):
                # Break at sentence ends once a cue is reasonably full: a cue that straddles two
                # sentences reads as one run-on line.
                flush()
        current.append(word)
    flush()

    # Merge cues too short to read into the following cue, so nothing flashes.
    merged: list[Cue] = []
    for cue in cues:
        if merged and cue.duration_ms < min_cue_ms:
            prev = merged[-1]
            prev.text = f"{prev.text} {cue.text}".strip()
            prev.end_ms = max(prev.end_ms, cue.end_ms)
            prev.word_count += cue.word_count
            continue
        merged.append(cue)

    # A zero-length cue cannot be displayed by any renderer; drop it and say so.
    final = [c for c in merged if c.end_ms > c.start_ms]
    captured = sum(c.word_count for c in final)
    return final, captured, truncated


def build_track(
    segments: list[dict],
    *,
    clip_start_ms: int,
    clip_end_ms: int,
    max_words_per_cue: int = 4,
    max_lines: int = 2,  # noqa: ARG001 - wrapping happens at serialisation, kept for the caller
) -> CaptionTrack:
    """Build a caption track for one clip window from stored transcript segments."""
    words, without_timing = words_from_segments(segments)
    in_range = [w for w in words if w.end_ms > clip_start_ms and w.start_ms < clip_end_ms]
    cues, captured, truncated = build_cues(
        words,
        clip_start_ms=clip_start_ms,
        clip_end_ms=clip_end_ms,
        max_words_per_cue=max_words_per_cue,
    )
    track = CaptionTrack(
        cues=cues,
        words_in_range=len(in_range),
        words_captioned=captured,
        words_without_timing=without_timing,
        truncated_at_clip_end=truncated,
    )
    if without_timing:
        track.warnings.append(
            f"{without_timing} word(s) had no timing information and were not captioned"
        )
    if in_range and not cues:
        track.warnings.append(
            "no caption cues could be built for this window; the clip will render without captions"
        )
    return track


# --------------------------------------------------------------------------- serialisation
def _vtt_ts(ms: int) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def _ass_ts(ms: int) -> str:
    """ASS uses centiseconds and a single-digit hour field."""
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:d}:{m:02d}:{s:02d}.{milli // 10:02d}"


def to_webvtt(cues: list[Cue]) -> str:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(f"{_vtt_ts(cue.start_ms)} --> {_vtt_ts(cue.end_ms)}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines)


def to_ass(
    cues: list[Cue],
    *,
    width: int,
    height: int,  # noqa: ARG001 - part of the canvas contract; positions use script space
    font_family: str = "DejaVu Sans",
    font_size_px: int = 64,
    text_color: str = "#FFFFFF",
    outline_px: int = 6,
    position: str = "lower_third",
    max_lines: int = 2,
) -> str:
    """Render an ASS subtitle file matching a template's caption style.

    ASS sizes are relative to a 384x288 script resolution, so pixel sizes from a 1080x1920
    template are scaled into that space. Getting this wrong makes captions either microscopic or
    enormous, which is why it is computed rather than guessed.
    """
    script_w, script_h = 384, 288
    style_size = max(8, round(font_size_px * script_w / width))
    outline = max(1, round(outline_px * script_w / width))

    # ASS positions from the anchor edge, so these are distances from the *bottom* for alignment 2.
    # A lower-third caption wants its text block sitting around 76% down the frame: with two lines
    # at `style_size`, a bottom margin of ~14% of the script height lands there. The previous
    # value (35%) pushed the block up into the middle of the picture.
    margin_v = {
        "lower_third": round(script_h * 0.14),
        "center": round(script_h * 0.40),
        "top": round(script_h * 0.10),
    }.get(position, round(script_h * 0.14))

    def ass_colour(hex_colour: str) -> str:
        h = hex_colour.lstrip("#")
        if len(h) != 6:
            h = "FFFFFF"
        r, g, b = h[0:2], h[2:4], h[4:6]
        return f"&H00{b}{g}{r}"  # ASS is &HAABBGGRR

    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {script_w}",
        f"PlayResY: {script_h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        (f"Style: Default,{font_family},{style_size},{ass_colour(text_color)},&H000000FF,"
        f"&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{outline},1,2,40,40,{margin_v},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events = []
    for cue in cues:
        wrapped = _wrap(cue.text, max_lines)
        events.append(
            f"Dialogue: 0,{_ass_ts(cue.start_ms)},{_ass_ts(cue.end_ms)},"
            f"Default,,0,0,0,,{wrapped}"
        )
    return "\n".join(header + events) + "\n"


def _wrap(text: str, max_lines: int) -> str:
    """Break a cue into at most `max_lines` lines with an explicit ASS line break."""
    words = text.split()
    if max_lines <= 1 or len(words) <= 3:
        return text
    mid = (len(words) + 1) // 2
    return " ".join(words[:mid]) + r"\N" + " ".join(words[mid:])


def write_captions(
    track: CaptionTrack,
    *,
    out_dir: Path,
    stem: str = "captions",
    width: int,
    height: int,
    font_family: str = "DejaVu Sans",
    font_size_px: int = 64,
    text_color: str = "#FFFFFF",
    outline_px: int = 6,
    position: str = "lower_third",
    max_lines: int = 2,
) -> tuple[Path, Path]:
    """Write both caption formats. Returns `(ass_path, vtt_path)`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ass_path = out_dir / f"{stem}.ass"
    vtt_path = out_dir / f"{stem}.vtt"
    ass_path.write_text(
        to_ass(
            track.cues,
            width=width,
            height=height,
            font_family=font_family,
            font_size_px=font_size_px,
            text_color=text_color,
            outline_px=outline_px,
            position=position,
            max_lines=max_lines,
        ),
        encoding="utf-8",
    )
    vtt_path.write_text(to_webvtt(track.cues), encoding="utf-8")
    return ass_path, vtt_path

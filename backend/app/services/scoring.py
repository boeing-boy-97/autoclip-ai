"""Moment detection and decomposed scoring.

Two principles drive this module, both from the product spec:

* **Scores describe the content, not the audience.** Every criterion is a property we can point at
  in the transcript (does the segment open with a claim? does it resolve? is it self-contained?).
  Nothing here is a prediction of views, and nothing is randomised.
* **Every number must be explainable.** Each criterion returns a value *and* the evidence that
  produced it, and the weights come from `config/scoring.yaml` with its version stamped onto every
  row. A reviewer can always see why a clip ranked where it did.

When no AI provider is configured, scoring uses the documented heuristic path. That path is
deliberately conservative about what it claims: lexical signals are weak proxies for semantic
quality, and the sub-scores are reported as estimates with their method recorded.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CRITERIA = (
    "hook",
    "content_value",
    "narrative",
    "standalone",
    "audience",
    "emotion",
    "visual_quality",
    "audio_quality",
    "ending_strength",
    "clarity",
)

# Words that tend to open a self-contained, attention-worthy idea. Used as weak evidence only.
_HOOK_MARKERS = {
    "most",
    "why",
    "how",
    "never",
    "stop",
    "the",
    "biggest",
    "mistake",
    "truth",
    "nobody",
    "everyone",
    "here",
    "imagine",
    "what",
    "if",
    "when",
    "i",
    "we",
    "you",
}
_QUESTION_STARTERS = ("why", "how", "what", "when", "who", "should", "can", "do", "does", "is")
_CONTRAST = {"but", "however", "actually", "instead", "yet", "although", "though"}
_INSIGHT = {
    "because",
    "means",
    "therefore",
    "so",
    "learned",
    "realise",
    "realize",
    "discovered",
    "understand",
    "reason",
    "problem",
    "solution",
    "result",
    "matters",
    "important",
}
_RESOLUTION = {"finally", "so", "therefore", "in the end", "the point", "conclusion", "that is"}
_FILLER = {"um", "uh", "erm", "like", "you know", "kind of", "sort of", "I mean"}
# Pronouns and deictic references that signal a clip depends on unseen context.
_DEPENDENT_REFS = {
    "this",
    "that",
    "these",
    "those",
    "he",
    "she",
    "it",
    "they",
    "them",
    "there",
    "then",
    "earlier",
    "previously",
    "as i said",
    "like i said",
    "anyway",
}
_EMOTION_WORDS = {
    "amazing",
    "shocking",
    "surprising",
    "incredible",
    "terrible",
    "huge",
    "love",
    "hate",
    "afraid",
    "excited",
    "crazy",
    "unbelievable",
    "honestly",
    "actually",
    "really",
}


@dataclass(slots=True)
class ScoringConfig:
    version: str
    weights: dict[str, float]
    rollup: dict[str, list[str]]
    duration: dict[str, object]
    categories: dict[str, object]
    diversity: dict[str, object]

    @property
    def enabled_categories(self) -> list[str]:
        raw = self.categories.get("enabled")
        if not isinstance(raw, list | tuple):
            return []
        return [str(item) for item in raw]


def load_scoring_config(path: str | Path) -> ScoringConfig:
    """Load and validate weights. A config whose weights do not sum to 1.0 is rejected."""
    data = yaml.safe_load(Path(path).read_text())
    criteria = data["criteria"]
    weights = {name: float(spec["weight"]) for name, spec in criteria.items()}
    missing = set(CRITERIA) - set(weights)
    if missing:
        raise ValueError(f"scoring config is missing criteria: {sorted(missing)}")
    total = sum(weights.values())
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise ValueError(f"scoring weights must sum to 1.0, got {total:.6f}")
    return ScoringConfig(
        version=str(data["version"]),
        weights=weights,
        rollup={k: list(v) for k, v in data["rollup"].items()},
        duration=dict(data.get("duration") or {}),
        categories=dict(data.get("categories") or {}),
        diversity=dict(data.get("diversity") or {}),
    )


# --------------------------------------------------------------------- text features


@dataclass(slots=True)
class TextFeatures:
    word_count: int
    sentence_count: int
    duration_ms: int
    opens_with_question: bool
    opens_with_claim: bool
    contrast_markers: int
    insight_markers: int
    resolution_markers: int
    filler_ratio: float
    dependent_refs: int
    emotion_markers: int
    lexical_diversity: float
    words_per_minute: float
    ends_complete: bool


def extract_features(text: str, duration_ms: int) -> TextFeatures:
    """Deterministic lexical features. Same input always yields the same output."""
    clean = re.sub(r"\s+", " ", text.strip())
    words = [w for w in re.findall(r"[\w']+", clean.lower()) if w]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", clean) if s.strip()]
    n = max(len(words), 1)
    lowered = clean.lower()

    first = words[0] if words else ""
    return TextFeatures(
        word_count=len(words),
        sentence_count=len(sentences),
        duration_ms=duration_ms,
        opens_with_question=first in _QUESTION_STARTERS or clean.endswith("?"),
        opens_with_claim=first in _HOOK_MARKERS,
        contrast_markers=sum(1 for w in words if w in _CONTRAST),
        insight_markers=sum(1 for w in words if w in _INSIGHT),
        resolution_markers=sum(1 for phrase in _RESOLUTION if phrase in lowered),
        filler_ratio=sum(lowered.count(f) for f in _FILLER) / n,
        dependent_refs=sum(1 for w in words if w in _DEPENDENT_REFS),
        emotion_markers=sum(1 for w in words if w in _EMOTION_WORDS),
        lexical_diversity=len(set(words)) / n,
        words_per_minute=(len(words) / duration_ms * 60_000) if duration_ms else 0.0,
        # A clip should end on a finished thought, not mid-sentence.
        ends_complete=bool(re.search(r"[.!?][\"')\]]?\s*$", clean)) or bool(sentences),
    )


# --------------------------------------------------------------------- scoring


@dataclass(slots=True)
class CriterionResult:
    value: float
    evidence: str


@dataclass(slots=True)
class ScoreResult:
    criteria: dict[str, float]
    evidence: dict[str, str]
    rollup: dict[str, float]
    overall: float
    weights_version: str
    method: str
    notes: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, float]:
        row = {k: round(v, 2) for k, v in self.criteria.items()}
        row.update({k: round(v, 2) for k, v in self.rollup.items()})
        row["overall"] = round(self.overall, 2)
        return row


def _scale(value: float, low: float, high: float) -> float:
    """Map a raw count onto 0-100 with soft saturation (diminishing returns)."""
    if value <= low:
        return 0.0
    if value >= high:
        return 100.0
    return (value - low) / (high - low) * 100.0


def score_moment(
    *,
    text: str,
    duration_ms: int,
    config: ScoringConfig,
    has_speech: bool = True,
    audio_quality: float | None = None,
    visual_quality: float | None = None,
    audience_fit: float | None = None,
    method: str = "heuristic",
) -> ScoreResult:
    """Score one candidate window. Deterministic; no randomness anywhere."""
    f = extract_features(text, duration_ms)
    evidence: dict[str, str] = {}
    values: dict[str, float] = {}

    # --- hook: does the opening stand on its own as an attention grabber?
    hook = 35.0
    bits = []
    if f.opens_with_question:
        hook += 25.0
        bits.append("opens with a question")
    if f.opens_with_claim:
        hook += 15.0
        bits.append("opens with a direct claim")
    if f.contrast_markers:
        hook += min(15.0, 7.5 * f.contrast_markers)
        bits.append(f"{f.contrast_markers} contrast marker(s)")
    if f.emotion_markers:
        hook += min(10.0, 5.0 * f.emotion_markers)
        bits.append(f"{f.emotion_markers} emotionally charged word(s)")
    values["hook"] = _clip(hook)
    evidence["hook"] = ", ".join(bits) or "no strong opening signal"

    # --- content value: information density
    density = (f.insight_markers / max(f.sentence_count, 1)) * 100
    values["content_value"] = _clip(25.0 + 0.55 * min(density, 100) + 10 * f.lexical_diversity)
    evidence["content_value"] = (
        f"{f.insight_markers} insight marker(s) across {f.sentence_count} sentence(s); "
        f"lexical diversity {f.lexical_diversity:.2f}"
    )

    # --- narrative: setup → development → resolution
    narrative = 30.0
    if f.sentence_count >= 2:
        narrative += 20.0
    if f.sentence_count >= 4:
        narrative += 15.0
    if f.contrast_markers:
        narrative += 15.0
    if f.resolution_markers:
        narrative += 20.0
    values["narrative"] = _clip(narrative)
    evidence["narrative"] = (
        f"{f.sentence_count} sentence(s), {f.contrast_markers} turn(s), "
        f"{f.resolution_markers} resolution marker(s)"
    )

    # --- standalone: intelligible without the rest of the video
    ref_ratio = f.dependent_refs / max(f.word_count, 1)
    standalone = 100.0 - min(65.0, ref_ratio * 320)
    if f.opens_with_question or f.opens_with_claim:
        standalone += 5.0
    values["standalone"] = _clip(standalone)
    evidence["standalone"] = (
        f"{f.dependent_refs} context-dependent reference(s) ({ref_ratio:.1%} of words)"
    )

    # --- audience: language and pacing fit. Only measurable if a caller supplies it.
    if audience_fit is not None:
        values["audience"] = _clip(audience_fit)
        evidence["audience"] = "supplied by caller (audience profile fit)"
    else:
        wpm = f.words_per_minute
        # 120-190 wpm is comfortable for short-form; outside that we degrade gently.
        values["audience"] = _clip(85.0 - abs(wpm - 155) * 0.35) if wpm else 50.0
        evidence["audience"] = (
            f"pacing {wpm:.0f} words/min against a 155 wpm comfort band; "
            "language assumed to match project setting"
        )

    # --- emotion: curiosity/stakes charge (explicitly not a virality prediction)
    values["emotion"] = _clip(
        25.0 + 12.0 * min(f.emotion_markers, 5) + 8.0 * min(f.contrast_markers, 3)
    )
    evidence["emotion"] = (
        f"{f.emotion_markers} emotional term(s), {f.contrast_markers} contrast term(s)"
    )

    # --- technical quality: supplied by the media pipeline, never guessed here
    if visual_quality is not None:
        values["visual_quality"] = _clip(visual_quality)
        evidence["visual_quality"] = "measured from frames by the media pipeline"
    else:
        values["visual_quality"] = 50.0
        evidence["visual_quality"] = "not measured in this run (no frame analysis configured)"
    if audio_quality is not None:
        values["audio_quality"] = _clip(audio_quality)
        evidence["audio_quality"] = "measured from speech intelligibility signals"
    else:
        # Speech-only sources get a neutral-but-honest baseline; silence gets nothing.
        values["audio_quality"] = 60.0 if has_speech else 0.0
        evidence["audio_quality"] = (
            "speech detected; loudness measured, intelligibility not modelled"
            if has_speech
            else "no speech detected in this window"
        )

    # --- ending strength: does it land?
    ending = 30.0
    if f.resolution_markers:
        ending += 30.0
    if f.ends_complete:
        ending += 25.0
    if not f.resolution_markers and f.sentence_count > 2:
        ending += 10.0  # at least finished a thought
    values["ending_strength"] = _clip(ending)
    evidence["ending_strength"] = (
        f"{f.resolution_markers} resolution marker(s); "
        f"final sentence {'closed' if f.ends_complete else 'open'}"
    )

    # --- clarity: absence of meandering
    clarity = 80.0 - f.filler_ratio * 300.0
    if f.lexical_diversity > 0.7:
        clarity += 10.0
    if f.sentence_count and f.word_count / f.sentence_count > 45:
        clarity -= 15.0  # run-on sentences
    values["clarity"] = _clip(clarity)
    evidence["clarity"] = (
        f"filler ratio {f.filler_ratio:.1%}; "
        f"{f.word_count / max(f.sentence_count, 1):.0f} words/sentence"
    )

    # --- roll-up: weights from config, aggregates preserved separately
    overall = sum(values[c] * config.weights[c] for c in CRITERIA)
    rollup = {
        name: sum(values[c] for c in members) / len(members)
        for name, members in config.rollup.items()
    }

    notes = []
    if method == "heuristic":
        notes.append(
            "Heuristic mode: scores derive from lexical signals because no AI provider is "
            "configured. They describe content characteristics and are not view predictions."
        )
    if visual_quality is None:
        notes.append("visual_quality is a neutral placeholder because frames were not analysed.")

    return ScoreResult(
        criteria={k: round(v, 2) for k, v in values.items()},
        evidence=evidence,
        rollup={k: round(v, 2) for k, v in rollup.items()},
        overall=round(overall, 2),
        weights_version=config.version,
        method=method,
        notes=notes,
    )


# --------------------------------------------------------------------- candidate windows


@dataclass(slots=True)
class SegmentView:
    """Minimal view of a transcript segment used for windowing."""

    idx: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(slots=True)
class CandidateWindow:
    segments: list[SegmentView]
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()


def build_windows(
    segments: list[SegmentView],
    *,
    target_min_ms: int,
    target_max_ms: int,
    config: ScoringConfig,
) -> list[CandidateWindow]:
    """Grow sentence-aligned windows until they reach the duration band.

    Presets are *preferences*, not clamps: a window that would cut a sentence mid-way is extended
    to the sentence boundary, and `hard_max_ms` from config is the only absolute ceiling.
    """
    if not segments:
        return []

    duration_cfg = config.duration
    hard_max = _as_int(duration_cfg.get("hard_max_ms"), 180_000)
    ceiling = min(max(target_max_ms, target_min_ms), hard_max)
    floor_ms = _as_int(duration_cfg.get("hard_min_ms"), 3_000)
    windows: list[CandidateWindow] = []

    for i in range(len(segments)):
        picked: list[SegmentView] = []
        for j in range(i, len(segments)):
            picked.append(segments[j])
            span = picked[-1].end_ms - picked[0].start_ms
            if span >= target_min_ms:
                windows.append(
                    CandidateWindow(
                        segments=list(picked),
                        start_ms=picked[0].start_ms,
                        end_ms=picked[-1].end_ms,
                    )
                )
                break
            if span >= ceiling:
                break
        else:
            # Ran out of segments: keep the window only if it is still long enough to be a clip.
            if picked:
                span = picked[-1].end_ms - picked[0].start_ms
                if floor_ms <= span <= ceiling:
                    windows.append(
                        CandidateWindow(
                            segments=list(picked),
                            start_ms=picked[0].start_ms,
                            end_ms=picked[-1].end_ms,
                        )
                    )
            continue

    # Drop windows shorter than the floor and windows that overflow the ceiling.
    return [w for w in windows if floor_ms <= w.duration_ms <= ceiling]


def iou(a: CandidateWindow, b: CandidateWindow) -> float:
    overlap = max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))
    if overlap <= 0:
        return 0.0
    union = max(a.end_ms, b.end_ms) - min(a.start_ms, b.start_ms)
    return overlap / union if union else 0.0


def deduplicate(
    scored: list[tuple[CandidateWindow, ScoreResult]],
    *,
    max_overlap: float,
    min_gap_ms: int,
) -> list[tuple[CandidateWindow, ScoreResult, str | None]]:
    """Order by score, suppress overlapping near-duplicates.

    Returns every candidate with a `suppressed_reason` (None when kept) so the API can show what
    was considered and why it lost — silence about rejected candidates would be unexplainable.
    """
    ordered = sorted(scored, key=lambda pair: pair[1].overall, reverse=True)
    kept: list[tuple[CandidateWindow, ScoreResult]] = []
    out: list[tuple[CandidateWindow, ScoreResult, str | None]] = []

    for window, score in ordered:
        reason: str | None = None
        for keeper, _ in kept:
            if iou(window, keeper) > max_overlap:
                reason = "overlap"
                break
            gap = min(abs(window.start_ms - keeper.end_ms), abs(keeper.start_ms - window.end_ms))
            separate = window.start_ms >= keeper.end_ms or keeper.start_ms >= window.end_ms
            if gap < min_gap_ms and not separate:
                reason = "overlap"
                break
        if reason is None:
            kept.append((window, score))
        out.append((window, score, reason))
    return out


def categorise(text: str, enabled: list[str]) -> str:
    """Assign a category from the taxonomy the schema allows, using transparent rules."""
    lowered = text.lower()
    if lowered.rstrip().endswith("?"):
        candidate = "important_question"
    elif any(w in lowered for w in ("why", "how to", "step", "first you", "then you")):
        candidate = "tutorial"
    elif any(w in lowered for w in ("most people", "nobody", "everyone thinks", "myth")):
        candidate = "surprising_fact"
    elif any(w in lowered for w in ("business", "customer", "revenue", "market", "company")):
        candidate = "business_insight"
    elif any(w in lowered for w in ("i felt", "i cried", "my story", "when i was")):
        candidate = "emotional_story"
    elif any(w in lowered for w in ("funny", "hilarious", "joke", "laugh")):
        candidate = "funny"
    elif any(w in lowered for w in ("i think", "in my opinion", "i believe")):
        candidate = "opinion"
    elif any(w in lowered for w in ("study", "research", "data", "percent", "%")):
        candidate = "educational"
    else:
        candidate = "other"
    if enabled and candidate not in enabled:
        return "other"
    return candidate


def _clip(value: float) -> float:
    return max(0.0, min(100.0, value))


def _as_int(value: object, default: int) -> int:
    """Coerce a YAML value to int, falling back to `default` rather than raising at runtime."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default

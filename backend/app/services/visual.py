"""Visual analysis: scene boundaries and measurable frame properties.

This stage exists to answer questions that only pixels can answer:

* Where does the picture change? (scene boundaries — used to snap clip edges to clean cuts)
* Is the framing usable for a 9:16 crop? (subject position, edge density)
* Is the image technically sound? (brightness, contrast, sharpness, black/frozen frames)

Everything here is measured from decoded frames with FFmpeg. There are no placeholder numbers:
if a metric cannot be computed, the column stays NULL and the reason is recorded in
`video_analysis.payload`, so downstream scoring knows the difference between "good" and "unknown".
"""

from __future__ import annotations

import re
import statistics
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

FFMPEG_TIMEOUT_SECONDS = 900


class VisualAnalysisError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class SceneBoundary:
    idx: int
    start_ms: int
    end_ms: int
    mean_motion: float | None = None


@dataclass(slots=True)
class FrameSample:
    at_ms: int
    brightness: float  # mean luma, 0-255
    contrast: float  # std-dev of luma
    sharpness: float  # mean |Laplacian|, higher = sharper


@dataclass(slots=True)
class VisualReport:
    scenes: list[SceneBoundary]
    frames: list[FrameSample]
    detector: str
    metrics: dict[str, float | None] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------- scene detection


def detect_scenes(
    path: Path,
    *,
    duration_ms: int,
    threshold: float = 0.30,
    ffmpeg_bin: str = "ffmpeg",
) -> list[SceneBoundary]:
    """Detect hard cuts with FFmpeg's scene score.

    `select='gt(scene,threshold)'` emits one record per frame whose scene-change score exceeds the
    threshold. The threshold is a *sensitivity*, not a promise: a single continuous shot correctly
    yields zero cuts, and we then report the whole video as one scene rather than inventing
    boundaries to look busy.
    """
    # `showinfo` prints its per-frame lines through the *info* log level. Running the filter
    # under `-v error` therefore suppressed every boundary and made this function silently report
    # "one scene" for every input — a bug that looks like a working detector.
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-loglevel", "info",
        "-i", str(path),
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-an",
        "-f", "null",
        "-",
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise VisualAnalysisError(
            "ffmpeg_unavailable", f"ffmpeg binary '{ffmpeg_bin}' not found"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VisualAnalysisError(
            "visual_analysis_timeout", "scene detection exceeded its time budget"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = stderr[-1] if stderr else "unknown error"
        raise VisualAnalysisError(
            "visual_analysis_failed", f"scene detection failed: {reason[:300]}"
        )

    output = completed.stderr.decode("utf-8", "replace")
    cut_times_ms = sorted(
        {round(float(m) * 1000) for m in re.findall(r"pts_time:([0-9.]+)", output)}
    )

    # Build scenes from the cut points, bounded by the real duration.
    edges = [0, *[t for t in cut_times_ms if 0 < t < duration_ms], duration_ms]
    scenes: list[SceneBoundary] = []
    for i in range(len(edges) - 1):
        start, end = edges[i], edges[i + 1]
        if end - start < 200:  # sub-200 ms fragments are detector noise, not shots
            continue
        scenes.append(SceneBoundary(idx=len(scenes), start_ms=start, end_ms=end))
    if not scenes:
        scenes = [SceneBoundary(idx=0, start_ms=0, end_ms=duration_ms)]
    return scenes


# --------------------------------------------------------------------- frame metrics


def sample_frames(
    path: Path,
    *,
    duration_ms: int,
    count: int = 8,
    ffmpeg_bin: str = "ffmpeg",
) -> list[FrameSample]:
    """Measure brightness, contrast and sharpness on evenly-spaced frames.

    The frames are decoded at a small, fixed size (320 px wide). That is enough to judge framing
    and exposure, cheap enough to run inline, and makes the sharpness values comparable across
    sources of different resolution.
    """
    if duration_ms <= 0:
        return []
    fps_expr = max(count / max(duration_ms / 1000, 1), 0.01)
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-v", "error",
        "-i", str(path),
        "-vf", f"fps={fps_expr:.6f},scale=320:-2,format=gray",
        "-frames:v", str(count),
        "-f", "rawvideo",
        "-",
    ]
    try:
        completed = subprocess.run(  # noqa: S603
            cmd, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise VisualAnalysisError(
            "ffmpeg_unavailable", f"ffmpeg binary '{ffmpeg_bin}' not found"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VisualAnalysisError(
            "visual_analysis_timeout", "frame sampling exceeded its time budget"
        ) from exc

    if completed.returncode != 0 or not completed.stdout:
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = stderr[-1] if stderr else "no frames decoded"
        raise VisualAnalysisError(
            "visual_analysis_failed", f"frame sampling failed: {reason[:300]}"
        )

    frame_size = _frame_bytes(completed.stdout)
    if frame_size == 0:
        raise VisualAnalysisError("visual_analysis_failed", "could not determine frame size")

    raw = np.frombuffer(completed.stdout, dtype=np.uint8)
    n_frames = raw.size // frame_size
    # Assume 320-wide frames; height inferred from the byte count of one frame.
    height = frame_size // 320
    samples: list[FrameSample] = []
    step = duration_ms / max(n_frames, 1)
    for i in range(n_frames):
        frame = raw[i * frame_size : (i + 1) * frame_size].reshape(height, 320).astype(np.float32)
        samples.append(
            FrameSample(
                at_ms=round(i * step),
                brightness=float(frame.mean()),
                contrast=float(frame.std()),
                sharpness=_laplacian_energy(frame),
            )
        )
    return samples


def _frame_bytes(data: bytes) -> int:
    """Infer bytes-per-frame for a 320-wide grayscale stream from the total payload."""
    total = len(data)
    for height in (180, 240, 320, 120, 360, 480, 96):
        size = 320 * height
        if total % size == 0:
            return size
    return 0


def _laplacian_energy(frame):  # noqa: ANN001, ANN202
    """Mean absolute 4-neighbour Laplacian — a standard cheap sharpness proxy."""
    k = (
        -4.0 * frame[1:-1, 1:-1]
        + frame[:-2, 1:-1]
        + frame[2:, 1:-1]
        + frame[1:-1, :-2]
        + frame[1:-1, 2:]
    )
    return float(np.abs(k).mean())


# --------------------------------------------------------------------- aggregate


def analyse_frames(path: Path, *, duration_ms: int, ffmpeg_bin: str = "ffmpeg") -> VisualReport:
    """Run the full visual pass and summarise it into 0-100 signals for scoring."""
    notes: list[str] = []
    scenes = detect_scenes(path, duration_ms=duration_ms, ffmpeg_bin=ffmpeg_bin)
    if len(scenes) == 1:
        notes.append("no hard cuts detected; treated as a single continuous shot")

    try:
        frames = sample_frames(path, duration_ms=duration_ms, ffmpeg_bin=ffmpeg_bin)
    except VisualAnalysisError as exc:
        # Frame metrics are an enhancement, not a prerequisite: report the gap honestly.
        frames = []
        notes.append(f"frame metrics unavailable: {exc.detail}")

    metrics: dict[str, float | None] = {
        "scene_count": float(len(scenes)),
        "mean_scene_ms": float(
            statistics.mean(s.end_ms - s.start_ms for s in scenes)
        )
        if scenes
        else None,
    }

    if frames:
        brightness = statistics.mean(f.brightness for f in frames)
        contrast = statistics.mean(f.contrast for f in frames)
        sharpness = statistics.mean(f.sharpness for f in frames)
        metrics.update(
            {
                "mean_brightness": round(brightness, 2),
                "mean_contrast": round(contrast, 2),
                "mean_sharpness": round(sharpness, 2),
                "min_brightness": round(min(f.brightness for f in frames), 2),
            }
        )
        # A frame that is near-black or near-white carries no information.
        black = sum(1 for f in frames if f.brightness < 16 or f.brightness > 240)
        metrics["flat_frame_ratio"] = round(black / len(frames), 3)
        if black:
            notes.append(f"{black}/{len(frames)} sampled frames were near-black or blown out")
        metrics["visual_quality_score"] = round(
            _visual_quality(brightness, contrast, sharpness, black / len(frames)), 2
        )
    else:
        for key in (
            "mean_brightness",
            "mean_contrast",
            "mean_sharpness",
            "min_brightness",
            "flat_frame_ratio",
            "visual_quality_score",
        ):
            metrics[key] = None

    return VisualReport(
        scenes=scenes, frames=frames, detector="ffmpeg_scene", metrics=metrics, notes=notes
    )


def _visual_quality(
    brightness: float, contrast: float, sharpness: float, flat_ratio: float
) -> float:
    """Map measured frame statistics onto a 0-100 quality signal.

    The bands are conventional video-engineering ranges, not learned taste:
    luma centred near 118 (mid-grey), contrast 40-70, sharpness above ~8 on the 320 px scale.
    """
    exposure = 100.0 - min(abs(brightness - 118.0) * 1.4, 100.0)
    contrast_score = min(contrast / 55.0, 1.0) * 100.0
    sharpness_score = min(sharpness / 12.0, 1.0) * 100.0
    penalty = flat_ratio * 100.0
    raw = exposure * 0.35 + contrast_score * 0.30 + sharpness_score * 0.35 - penalty
    return max(0.0, min(100.0, raw))

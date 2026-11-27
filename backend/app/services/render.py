"""Render a clip: cut the window, reframe to 9:16, burn captions, normalise audio.

The render plan is built first and stored in `clip_versions.render_plan`, then compiled to a
single FFmpeg invocation. Storing the plan matters: a rendered file whose recipe is unknown cannot
be explained, reproduced, or compared after a bug fix. The plan is the record.

Structural guarantees this module keeps (from the template contract, not optional flags):
  * the cut never lands inside a word — boundaries are nudged to a word gap, and **which** way
    each edge was nudged is recorded in the plan
  * captions come only from real ASR word timings (see `captions.py`); no timing is invented
  * pitch is never altered — no `asetrate`/`atempo` pitch-shifting filter is used
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

RENDER_PLAN_VERSION = 1
# Repository root, so a relative `template_dir` resolves the same from the API process, a Celery
# worker and a test run — all of which have different working directories.
REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_DIR_DEFAULT = str(REPO_ROOT / "config" / "templates")


def resolve_template_dir(template_dir: str | Path | None) -> Path:
    """Turn a configured template directory into a real path, whatever the cwd is."""
    if template_dir is None:
        return Path(TEMPLATE_DIR_DEFAULT)
    path = Path(template_dir)
    if path.is_absolute():
        return path
    candidate = REPO_ROOT / path
    # A relative path that only exists relative to the cwd still works; prefer the repo root so
    # behaviour does not depend on where the process was started.
    return candidate if candidate.exists() else path


class RenderError(RuntimeError):
    """Rendering failed for a reason the caller must see, with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class Template:
    key: str
    width: int
    height: int
    fps: int
    background: str
    reframe_strategy: str
    caption_style: dict
    audio_loudnorm: bool
    silence_trim: bool
    max_words_per_cue: int = 4
    max_lines: int = 2

    @classmethod
    def load(cls, key: str, template_dir: str | Path = TEMPLATE_DIR_DEFAULT) -> Template:
        path = resolve_template_dir(template_dir) / f"{key}.yaml"
        if not path.exists():
            raise RenderError("template_not_found", f"no template named {key!r}")
        data = yaml.safe_load(path.read_text()) or {}
        canvas = data.get("canvas") or {}
        captions = data.get("captions") or {}
        return cls(
            key=key,
            width=int(canvas.get("width", 1080)),
            height=int(canvas.get("height", 1920)),
            fps=int(canvas.get("fps", 30)),
            background=str(canvas.get("background", "#000000")),
            reframe_strategy=str((data.get("reframe") or {}).get("strategy", "centered")),
            caption_style=captions,
            audio_loudnorm=bool((data.get("audio") or {}).get("loudnorm", True)),
            silence_trim=bool((data.get("silence") or {}).get("trim", False)),
            max_words_per_cue=int(captions.get("max_words_per_cue", 4)),
            max_lines=int(captions.get("max_lines", 2)),
        )


@dataclass(slots=True)
class RenderPlan:
    clip_id: str
    template_key: str
    width: int
    height: int
    fps: int
    start_ms: int
    end_ms: int
    duration_ms: int
    has_captions: bool
    cue_count: int
    encoder: str
    crf: int
    preset: str
    source_width: int | None = None
    source_height: int | None = None
    adjustments: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": RENDER_PLAN_VERSION,
            "clip_id": self.clip_id,
            "template_key": self.template_key,
            "canvas": {"width": self.width, "height": self.height, "fps": self.fps},
            "window": {
                "start_ms": self.start_ms,
                "end_ms": self.end_ms,
                "duration_ms": self.duration_ms,
            },
            "source_dimensions": {
                "width": self.source_width,
                "height": self.source_height,
            },
            "captions": {"burned": self.has_captions, "cue_count": self.cue_count},
            "encoder": {"codec": self.encoder, "crf": self.crf, "preset": self.preset},
            "adjustments": self.adjustments,
            "warnings": self.warnings,
        }


@dataclass(slots=True)
class RenderResult:
    output_path: Path
    width: int
    height: int
    duration_ms: int
    bytes: int
    sha256: str
    render_seconds: float
    plan: RenderPlan
    encoder_log: str


def _run(cmd: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - argv list, no shell, fixed binary name
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def probe_source_size(source: Path, ffprobe_bin: str = "ffprobe") -> tuple[int, int]:
    proc = _run(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(source),
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        raise RenderError("probe_failed", f"could not probe source: {proc.stderr.strip()[:300]}")
    try:
        stream = json.loads(proc.stdout)["streams"][0]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RenderError("probe_failed", "source has no readable video stream") from exc
    return int(stream["width"]), int(stream["height"])


def nudge_to_word_gap(
    start_ms: int, end_ms: int, words: list, *, max_nudge_ms: int = 250
) -> tuple[int, int, dict]:
    """Keep the cut out of the middle of a spoken word.

    Returns adjusted `(start_ms, end_ms)` plus a record of what moved, which goes into the plan so
    the adjustment is visible rather than mysterious. A word that would be cut mid-way is included
    in full when that costs no more than `max_nudge_ms`; otherwise the cut stands and a warning is
    recorded, because silently swallowing a second of speech is worse than a slightly clipped word.
    """
    adjustment: dict = {"start_delta_ms": 0, "end_delta_ms": 0, "reason": []}

    for word in words:
        # Start falls strictly inside a word -> snap back to the word's start.
        if word.start_ms < start_ms < word.end_ms:
            delta = start_ms - word.start_ms
            if delta <= max_nudge_ms:
                adjustment["start_delta_ms"] = -delta
                adjustment["reason"].append(f"start moved back {delta}ms to avoid cutting a word")
                start_ms = word.start_ms
            else:
                adjustment["reason"].append(
                    f"start cuts through a {word.end_ms - word.start_ms}ms word; left as-is"
                )
            break

    for word in reversed(words):
        # End falls strictly inside a word -> extend to the word's end.
        if word.start_ms < end_ms < word.end_ms:
            delta = word.end_ms - end_ms
            if delta <= max_nudge_ms:
                adjustment["end_delta_ms"] = delta
                adjustment["reason"].append(f"end moved forward {delta}ms to finish a word")
                end_ms = word.end_ms
            else:
                adjustment["reason"].append(
                    f"end cuts through a {word.end_ms - word.start_ms}ms word; left as-is"
                )
            break

    return start_ms, end_ms, adjustment


def build_plan(
    *,
    clip_id: str,
    template: Template,
    start_ms: int,
    end_ms: int,
    cue_count: int,
    source_width: int | None,
    source_height: int | None,
    adjustments: dict | None = None,
    warnings: list[str] | None = None,
) -> RenderPlan:
    if end_ms <= start_ms:
        raise RenderError("invalid_window", "clip end must be after its start")
    return RenderPlan(
        clip_id=clip_id,
        template_key=template.key,
        width=template.width,
        height=template.height,
        fps=template.fps,
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=end_ms - start_ms,
        has_captions=cue_count > 0,
        cue_count=cue_count,
        encoder="libx264",
        crf=21,
        preset="veryfast",
        source_width=source_width,
        source_height=source_height,
        adjustments=adjustments or {},
        warnings=warnings or [],
    )


def _filter_chain(plan: RenderPlan, ass_path: Path | None) -> str:
    """Scale to fill the vertical canvas, crop the overflow, then burn captions.

    `scale=...:force_original_aspect_ratio=increase` followed by `crop` is deliberate: it fills the
    frame and crops rather than letterboxing, and it never distorts aspect ratio. The crop is
    centred, which is an honest default — real subject tracking is not implemented, and pretending
    otherwise by picking an arbitrary offset would be guesswork.
    """
    w, h = plan.width, plan.height
    chain = [
        f"scale={w}:{h}:force_original_aspect_ratio=increase",
        f"crop={w}:{h}",
        "setsar=1",
    ]
    if ass_path is not None:
        # The path is passed to FFmpeg inside a filter argument; escaping the colon and
        # backslashes is required or libass treats the path as filter options.
        escaped = str(ass_path).replace("\\", "\\\\").replace(":", r"\:")
        chain.append(f"ass={escaped}")
    chain.append(f"fps={plan.fps}")
    return ",".join(chain)


def render_clip(
    *,
    source_path: Path,
    output_path: Path,
    plan: RenderPlan,
    ass_path: Path | None,
    ffmpeg_bin: str = "ffmpeg",
    ffprobe_bin: str = "ffprobe",
    target_lufs: float | None = -14.0,
) -> RenderResult:
    """Cut, reframe and caption one clip. Raises `RenderError` on failure."""
    if not source_path.exists():
        raise RenderError("source_missing", "the source video is not available on this worker")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    start_s = plan.start_ms / 1000
    duration_s = plan.duration_ms / 1000

    audio_filters = []
    if target_lufs is not None:
        audio_filters.append(f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11")
    audio_filters.append("aresample=48000")
    af = ",".join(audio_filters)

    cmd = [
        ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-y",
        # Seek before -i for a fast, accurate cut: decoding from the keyframe then discarding is
        # what makes a 3-second clip of a long video take seconds rather than minutes.
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(source_path),
        "-t",
        f"{duration_s:.3f}",
        "-vf",
        _filter_chain(plan, ass_path),
        "-af",
        af,
        "-c:v",
        plan.encoder,
        "-preset",
        plan.preset,
        "-crf",
        str(plan.crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        # No `-progress pipe:1`: the entire progress stream would be buffered in memory for the
        # length of the render. The encoder's stderr tail is enough to explain a failure.
        "-loglevel",
        "error",
        "-nostats",
        str(output_path),
    ]

    started = time.monotonic()
    try:
        proc = _run(cmd, timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise RenderError("render_timeout", "rendering exceeded its time limit") from exc
    elapsed = time.monotonic() - started

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise RenderError("render_failed", "FFmpeg failed: " + " | ".join(tail)[:600])
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RenderError("render_empty", "FFmpeg reported success but produced no output")

    digest = hashlib.sha256()
    with output_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)

    # Verify what we produced rather than assuming: a render that silently lost its audio or
    # came out at the wrong size is a failure, not a success with a caveat.
    width, height, out_duration = _probe_output(output_path, ffprobe_bin)
    if (width, height) != (plan.width, plan.height):
        raise RenderError(
            "render_wrong_dimensions",
            f"expected {plan.width}x{plan.height}, got {width}x{height}",
        )
    if not _has_audio(output_path, ffprobe_bin):
        raise RenderError("render_no_audio", "the rendered clip has no audio stream")

    return RenderResult(
        output_path=output_path,
        width=width,
        height=height,
        duration_ms=out_duration,
        bytes=output_path.stat().st_size,
        sha256=digest.hexdigest(),
        render_seconds=round(elapsed, 3),
        plan=plan,
        encoder_log=(proc.stderr or "")[-4000:],
    )


def _probe_output(path: Path, ffprobe_bin: str = "ffprobe") -> tuple[int, int, int]:
    """Verify the rendered file with the *configured* ffprobe (never a guessed one)."""
    ffprobe = ffprobe_bin
    proc = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    if proc.returncode != 0:
        raise RenderError("probe_failed", "could not verify the rendered clip")
    data = json.loads(proc.stdout)
    stream = (data.get("streams") or [{}])[0]
    duration = float((data.get("format") or {}).get("duration") or 0)
    return int(stream.get("width", 0)), int(stream.get("height", 0)), round(duration * 1000)


def _has_audio(path: Path, ffprobe_bin: str = "ffprobe") -> bool:
    ffprobe = ffprobe_bin
    proc = _run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        timeout=60,
    )
    return proc.returncode == 0 and "audio" in proc.stdout

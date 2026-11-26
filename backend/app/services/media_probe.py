"""Media inspection: validate a file is genuinely a supported video, and record its facts.

Order of checks (each one cheap before the expensive next):
  1. extension allowlist
  2. declared MIME allowlist
  3. magic-byte / container sniff — the file must actually be what it claims
  4. `ffprobe` with a timeout and a restricted protocol whitelist
  5. semantic consistency (duration, streams, sane dimensions)

Why `-protocol_whitelist file`: ffprobe can follow URLs and playlists embedded in a container.
Without restriction, an uploaded file can make the server issue outbound requests (SSRF).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
ALLOWED_MIMES = {
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm",
    "video/x-m4v", "video/x-msvideo", "application/octet-stream",
}
# Container signatures (ISO-BMFF brands cover mp4/mov/m4v; Matroska/WebM share an EBML header).
_ISO_BMFF = (b"ftyp",)
_EBML = b"\x1a\x45\xdf\xa3"
_AVI = b"RIFF"

PROBE_TIMEOUT_SECONDS = 30


class MediaValidationError(ValueError):
    """Rejection with a machine-readable code mapped to HTTP 415/422 by the API."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class ProbeResult:
    duration_ms: int
    width: int
    height: int
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    audio_channels: int | None
    audio_sample_rate: int | None
    container: str | None
    raw: dict = field(default_factory=dict)


def sniff_container(head: bytes) -> str | None:
    """Identify a container from the first bytes. Returns None if unrecognised."""
    if len(head) < 12:
        return None
    if head[4:8] in _ISO_BMFF:
        return "iso_bmff"
    if head.startswith(_EBML):
        return "ebml"  # matroska / webm
    if head.startswith(_AVI):
        return "riff"  # avi
    return None


def validate_extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise MediaValidationError(
            "unsupported_media",
            f"unsupported file extension '{ext or '(none)'}'; allowed: "
            f"{', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    return ext


def validate_mime(declared: str | None) -> None:
    if declared and declared.split(";")[0].strip().lower() not in ALLOWED_MIMES:
        raise MediaValidationError(
            "unsupported_media",
            f"declared content type '{declared}' is not a supported video type",
        )


def validate_magic(head: bytes, ext: str) -> None:
    """The file's actual bytes must be consistent with its extension."""
    container = sniff_container(head)
    if container is None:
        raise MediaValidationError(
            "unsupported_media",
            "file does not look like a video container (no MP4/MOV, Matroska/WebM or AVI "
            "signature found); it may be renamed or corrupted",
        )
    mismatched = {
        ".avi": "riff",
        ".mkv": "ebml",
        ".webm": "ebml",
        ".mp4": "iso_bmff",
        ".mov": "iso_bmff",
        ".m4v": "iso_bmff",
    }
    expected = mismatched.get(ext)
    if expected and expected != container:
        raise MediaValidationError(
            "unsupported_media",
            f"file extension '{ext}' does not match its actual container signature",
        )


def _run_ffprobe(path: Path, ffprobe_bin: str) -> dict:
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        # Restrict protocols so a crafted container cannot make ffprobe fetch remote resources.
        "-protocol_whitelist", "file",
        str(path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise MediaValidationError(
            "ffprobe_unavailable",
            f"ffprobe binary '{ffprobe_bin}' not found on this host",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaValidationError(
            "unsupported_media", "media inspection timed out; the file may be malformed"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = stderr[-1] if stderr else "unknown error"
        raise MediaValidationError(
            "unsupported_media", f"file could not be decoded as video: {reason[:300]}"
        )
    try:
        return json.loads(completed.stdout or b"{}")
    except json.JSONDecodeError as exc:
        raise MediaValidationError(
            "unsupported_media", "ffprobe returned unparseable output"
        ) from exc


def probe_video(
    path: Path,
    *,
    ffprobe_bin: str = "ffprobe",
    max_duration_ms: int | None = None,
    require_audio: bool = True,
) -> ProbeResult:
    """Inspect a real media file and return validated facts. Raises MediaValidationError."""
    if not path.exists() or path.stat().st_size == 0:
        raise MediaValidationError("unsupported_media", "file is empty or missing")

    data = _run_ffprobe(path, ffprobe_bin)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        raise MediaValidationError("unsupported_media", "no video stream found")

    fmt = data.get("format") or {}
    duration_s = _to_float(fmt.get("duration"))
    if duration_s is None:
        # Some containers only report duration on the stream.
        duration_s = _to_float(video.get("duration"))
    if not duration_s or duration_s <= 0:
        raise MediaValidationError(
            "unsupported_media", "video duration is missing or zero — the file is likely truncated"
        )
    duration_ms = int(duration_s * 1000)
    if max_duration_ms and duration_ms > max_duration_ms:
        raise MediaValidationError(
            "too_long",
            f"video is {duration_ms // 60000} min; the limit is {max_duration_ms // 60000} min",
        )

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaValidationError("unsupported_media", "video dimensions are invalid")
    if height < 240 or width < 240:
        raise MediaValidationError(
            "unsupported_media", f"resolution {width}x{height} is too low to produce 9:16 output"
        )

    if require_audio and audio is None:
        raise MediaValidationError(
            "no_audio_stream",
            "no audio stream found; transcription and captions require audio",
        )

    fps = _parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if fps and fps < 10:
        raise MediaValidationError(
            "unsupported_media", f"frame rate {fps:.1f} fps is too low for video output"
        )

    return ProbeResult(
        duration_ms=duration_ms,
        width=width,
        height=height,
        fps=fps,
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=audio.get("channels") if audio else None,
        audio_sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        container=fmt.get("format_name"),
        raw=data,
    )


def ffmpeg_available(ffprobe_bin: str = "ffprobe") -> bool:
    return shutil.which(ffprobe_bin) is not None


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_fps(value: str | None) -> float | None:
    if not value or "/" not in value:
        return _to_float(value)
    num, _, den = value.partition("/")
    try:
        denominator = float(den)
        return float(num) / denominator if denominator else None
    except (TypeError, ValueError):
        return None

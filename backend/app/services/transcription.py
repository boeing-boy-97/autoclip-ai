"""Speech-to-text.

Two hard rules this module enforces:

1. **No invented transcripts.** If the ASR engine is missing, misconfigured, or produces no
   speech, the result says exactly that. There is no code path that synthesises plausible-looking
   segments, guesses timings, or fills a gap with placeholder text.
2. **Provider facts are preserved.** Segment and word timings come from the engine; we convert
   units and clamp to the media duration, but we never nudge a timestamp to make output prettier.

Engines
-------
* `faster-whisper` (default, local, CPU-capable). Model size is `ASR_MODEL`.
* Hosted providers are *not* wired in yet. Selecting one raises `AsrUnavailable` naming the exact
  missing credential, rather than silently falling back to a different engine (the fallback would
  change language handling and cost without telling anyone).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import Settings

# `numpy` and `wave` are used to decode the canonical WAV we produce. Both ship with the ASR
# dependency set (faster-whisper depends on numpy), so they are imported at module level.
try:
    import wave

    import numpy as np
except ImportError:  # pragma: no cover - only on a host without the ASR extras
    wave = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


class AsrUnavailableError(RuntimeError):
    """The configured engine cannot run here. Carries a machine code the API can report."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class AsrFailedError(RuntimeError):
    """The engine ran but could not produce a usable transcript (e.g. no speech detected)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class Word:
    text: str
    start_ms: int
    end_ms: int
    probability: float | None = None

    def as_dict(self) -> dict:
        return {
            "w": self.text,
            "s": self.start_ms,
            "e": self.end_ms,
            "p": round(self.probability, 4) if self.probability is not None else None,
        }


@dataclass(slots=True)
class Segment:
    idx: int
    start_ms: int
    end_ms: int
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    words: list[Word] = field(default_factory=list)


@dataclass(slots=True)
class TranscriptionResult:
    provider: str
    model: str
    language: str | None
    segments: list[Segment]
    word_timestamps: bool
    confidence: float | None
    duration_ms: int
    elapsed_seconds: float
    raw: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments).strip()

    @property
    def word_count(self) -> int:
        return sum(len(s.words) for s in self.segments) or len(self.text.split())


# --------------------------------------------------------------------- audio preparation


def extract_asr_audio(
    source_path: Path,
    out_path: Path,
    *,
    ffmpeg_bin: str = "ffmpeg",
    target_lufs: float = -14.0,
    timeout_seconds: int = 1800,
) -> Path:
    """Extract a 16 kHz mono WAV, loudness-normalised for the ASR engine.

    Whisper-family models expect 16 kHz mono. Normalisation is a *transcription quality* step, not
    an aesthetic one; the untouched audio is separately retained as `audio_master` by the caller.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-protocol_whitelist",
        "file",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-af",
        f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
        str(out_path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, timeout=timeout_seconds, check=False
        )
    except FileNotFoundError as exc:
        raise AsrUnavailableError(
            "ffmpeg_unavailable", f"ffmpeg binary '{ffmpeg_bin}' not found on this host"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AsrFailedError(
            "audio_extraction_timeout", "audio extraction exceeded its time budget"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = stderr[-1] if stderr else "unknown error"
        raise AsrFailedError(
            "audio_extraction_failed", f"ffmpeg could not extract audio: {reason[:300]}"
        )
    # A 44-byte file is just the WAV header, i.e. no samples were written.
    if not out_path.exists() or out_path.stat().st_size <= 44:
        raise AsrFailedError(
            "audio_extraction_failed",
            "audio extraction produced no samples; the source may have no audio stream",
        )
    return out_path


# --------------------------------------------------------------------- local engine


_MODEL_CACHE: dict[tuple[str, str, str], object] = {}


def _load_model(settings: Settings):  # noqa: ANN202
    """Load (and memoise) the faster-whisper model. Download happens once, then it is local."""
    try:
        from faster_whisper import WhisperModel  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise AsrUnavailableError(
            "asr_engine_missing",
            "ASR_PROVIDER=local requires the 'faster-whisper' package, which is not installed."
            " Install it (pip install faster-whisper) or configure a hosted provider.",
        ) from exc

    key = (settings.asr_model, settings.asr_device, settings.asr_compute_type)
    if key not in _MODEL_CACHE:
        try:
            _MODEL_CACHE[key] = WhisperModel(
                settings.asr_model,
                device=settings.asr_device,
                compute_type=settings.asr_compute_type,
                download_root=settings.asr_model_dir,
            )
        except Exception as exc:
            raise AsrUnavailableError(
                "asr_model_unavailable",
                f"could not load ASR model '{settings.asr_model}': {exc}",
            ) from exc
    return _MODEL_CACHE[key]


def transcribe_local(
    audio_path: Path,
    *,
    settings: Settings,
    language: str | None = None,
    duration_ms: int | None = None,
    task: str = "transcribe",
) -> TranscriptionResult:
    """Run faster-whisper over a 16 kHz mono WAV and return provider-accurate timings."""
    if not audio_path.exists():
        raise AsrFailedError("audio_missing", f"audio file not found: {audio_path.name}")

    model = _load_model(settings)
    started = time.monotonic()
    samples = _read_16k_mono(audio_path)

    try:
        segments_iter, info = model.transcribe(
            samples,
            language=language,
            task=task,
            beam_size=settings.asr_beam_size,
            vad_filter=settings.asr_vad_filter,
            word_timestamps=True,
            condition_on_previous_text=False,  # bounds hallucination drift on long inputs
        )
        raw_segments = list(segments_iter)
    except Exception as exc:
        raise AsrFailedError("asr_failed", f"ASR engine failed: {exc}") from exc

    limit_ms = duration_ms if duration_ms else None
    segments: list[Segment] = []
    for seg in raw_segments:
        text = (seg.text or "").strip()
        if not text:
            continue  # VAD-flagged silence yields empty text; never persist it
        start_ms = _clamp(round(seg.start * 1000), 0, limit_ms)
        end_ms = _clamp(round(seg.end * 1000), start_ms + 1, limit_ms)
        words: list[Word] = []
        for w in getattr(seg, "words", None) or []:
            w_start = _clamp(round(w.start * 1000), start_ms, end_ms)
            w_end = _clamp(round(w.end * 1000), w_start + 1, end_ms)
            words.append(
                Word(
                    text=(w.word or "").strip(),
                    start_ms=w_start,
                    end_ms=w_end,
                    probability=getattr(w, "probability", None),
                )
            )
        segments.append(
            Segment(
                idx=len(segments),
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                avg_logprob=_to_float(getattr(seg, "avg_logprob", None)),
                no_speech_prob=_to_float(getattr(seg, "no_speech_prob", None)),
                words=words,
            )
        )

    elapsed = time.monotonic() - started
    if not segments:
        raise AsrFailedError(
            "no_speech_detected",
            "the ASR engine found no speech in the source audio; nothing was transcribed",
        )

    probs = [w.probability for s in segments for w in s.words if w.probability is not None]
    confidence = sum(probs) / len(probs) if probs else None

    return TranscriptionResult(
        provider="faster-whisper",
        model=settings.asr_model,
        language=getattr(info, "language", None) or language,
        segments=segments,
        word_timestamps=all(s.words for s in segments),
        confidence=confidence,
        duration_ms=round((getattr(info, "duration", 0) or 0) * 1000),
        elapsed_seconds=round(elapsed, 3),
        raw={
            "language_probability": _to_float(getattr(info, "language_probability", None)),
            "duration": _to_float(getattr(info, "duration", None)),
            "model": settings.asr_model,
            "beam_size": settings.asr_beam_size,
            "vad_filter": settings.asr_vad_filter,
            "segment_count": len(segments),
        },
    )


def _read_16k_mono(path: Path):  # noqa: ANN202
    """Decode our own 16 kHz mono PCM WAV into float32 samples.

    We deliberately do not hand a file path to the ASR engine: its decoder (PyAV) has had
    breaking signature changes, and a version mismatch then fails at the least convenient moment.
    We already produced a canonical WAV with ffmpeg, so reading it with the standard library is
    both dependency-free and exact — int16 PCM / 32768 is the documented input format.
    """
    if wave is None or np is None:
        raise AsrUnavailableError(
            "asr_engine_missing",
            "numpy is required to decode audio for the local ASR engine but is not installed",
        )
    try:
        with wave.open(str(path)) as wav:
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) != (1, 16000, 2):
                raise AsrFailedError(
                    "audio_format_unsupported",
                    f"expected 16 kHz mono 16-bit PCM, got {wav.getframerate()} Hz / "
                    f"{wav.getnchannels()} ch / {wav.getsampwidth() * 8}-bit",
                )
            frames = wav.readframes(wav.getnframes())
    except wave.Error as exc:
        raise AsrFailedError("audio_format_unsupported", f"not a readable PCM WAV: {exc}") from exc

    if not frames:
        raise AsrFailedError("audio_missing", "audio file contains no samples")
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


# --------------------------------------------------------------------- dispatch


HOSTED_PROVIDERS = {
    "openai": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "assemblyai": "ASSEMBLYAI_API_KEY",
}


def transcribe(
    audio_path: Path,
    *,
    settings: Settings,
    language: str | None = None,
    duration_ms: int | None = None,
) -> TranscriptionResult:
    """Transcribe with the configured provider, or refuse with a precise reason."""
    provider = settings.asr_provider

    if provider == "local":
        return transcribe_local(
            audio_path, settings=settings, language=language, duration_ms=duration_ms
        )

    if provider in HOSTED_PROVIDERS:
        env_name = HOSTED_PROVIDERS[provider]
        if not getattr(settings, "openai_api_key", None) and env_name == "OPENAI_API_KEY":
            raise AsrUnavailableError(
                "asr_provider_not_configured",
                f"ASR_PROVIDER={provider} requires {env_name} to be set; no credential is "
                "configured on this host. Nothing was transcribed.",
            )
        raise AsrUnavailableError(
            "asr_provider_unsupported",
            f"ASR_PROVIDER={provider} is declared in configuration but its adapter is not "
            "implemented yet. Use ASR_PROVIDER=local, which runs a real local engine.",
        )

    raise AsrUnavailableError("asr_provider_invalid", f"unknown ASR_PROVIDER '{provider}'")


def _clamp(value: int, low: int, high: int | None) -> int:
    if high is None:
        return max(value, low)
    return max(low, min(value, high))


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

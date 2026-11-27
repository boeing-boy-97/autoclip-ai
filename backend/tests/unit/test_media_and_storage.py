"""Unit tests for media validation, storage keys and cursor encoding.

These are fast and need no database or network, but they exercise the real FFmpeg binary on real
fixtures — the point is to prove the *validation logic*, not to mock the media tooling.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.db.repositories import ConflictError, _decode_cursor, _encode_cursor
from app.services.media_probe import (
    MediaValidationError,
    probe_video,
    sniff_container,
    validate_extension,
    validate_magic,
    validate_mime,
)
from app.services.storage import build_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "generated"


def fixture(name: str) -> Path:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"media fixture missing: {name} (run ops/scripts/make_fixtures.sh)")
    return path


# --------------------------------------------------------------------- extension / mime


@pytest.mark.parametrize("name", ["a.mp4", "b.MP4", "c.mov", "d.mkv", "e.webm", "f.m4v", "g.avi"])
def test_allowed_extensions(name: str):
    assert validate_extension(name).startswith(".")


@pytest.mark.parametrize("name", ["a.exe", "b.txt", "c.wav", "d", "e.mp3", "f.mp4.exe"])
def test_rejected_extensions(name: str):
    with pytest.raises(MediaValidationError) as exc:
        validate_extension(name)
    assert exc.value.code == "unsupported_media"


def test_mime_allowlist():
    validate_mime("video/mp4")
    validate_mime("video/mp4; codecs=avc1")
    validate_mime(None)  # absent is fine; magic bytes still decide
    with pytest.raises(MediaValidationError):
        validate_mime("text/html")


# --------------------------------------------------------------------- magic bytes


def test_sniff_iso_bmff():
    assert sniff_container(b"\x00\x00\x00\x18ftypisom") == "iso_bmff"


def test_sniff_ebml_and_riff():
    assert sniff_container(b"\x1a\x45\xdf\xa3\x01\x00\x00\x00\x00\x00\x00\x00") == "ebml"
    assert sniff_container(b"RIFF\x00\x00\x00\x00AVI LIST") == "riff"


def test_sniff_rejects_short_or_unknown():
    assert sniff_container(b"\x00\x00") is None
    assert sniff_container(b"this is plain text, not a container") is None


def test_magic_must_match_extension():
    mp4_head = b"\x00\x00\x00\x18ftypisom"
    validate_magic(mp4_head, ".mp4")  # fine
    with pytest.raises(MediaValidationError):
        validate_magic(mp4_head, ".mkv")  # extension lies about the container
    with pytest.raises(MediaValidationError):
        validate_magic(b"plain text" * 100, ".mp4")


# --------------------------------------------------------------------- real probing


def test_probe_reports_real_facts():
    """Ground truth: make_fixtures.sh encodes talking_30s.mp4 as 640x360 h264/aac for 30 s."""
    result = probe_video(fixture("talking_30s.mp4"))
    assert result.duration_ms == pytest.approx(30_000, abs=200)
    assert (result.width, result.height) == (640, 360)
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert result.audio_channels == 1
    assert result.fps == pytest.approx(24, abs=1)  # fixtures encode at 24 fps
    assert "mp4" in (result.container or "")


def test_probe_rejects_corrupt_file():
    with pytest.raises(MediaValidationError) as exc:
        probe_video(fixture("corrupt.mp4"))
    assert exc.value.code == "unsupported_media"
    assert "decode" in exc.value.detail or "Invalid" in exc.value.detail


def test_probe_enforces_duration_limit():
    with pytest.raises(MediaValidationError) as exc:
        probe_video(fixture("talking_30s.mp4"), max_duration_ms=5_000)
    assert exc.value.code == "too_long"


def test_probe_requires_audio():
    """square.mp4 has audio; a video-only file would be refused with no_audio_stream."""
    result = probe_video(fixture("square.mp4"))
    assert result.audio_codec is not None


def test_probe_missing_binary_is_reported_not_faked():
    with pytest.raises(MediaValidationError) as exc:
        probe_video(fixture("silent.mp4"), ffprobe_bin="definitely-not-ffprobe")
    assert exc.value.code == "ffprobe_unavailable"


# --------------------------------------------------------------------- storage keys


def test_build_key_has_no_user_input():
    ws, proj = uuid.uuid4(), uuid.uuid4()
    key = build_key(
        workspace_id=ws,
        project_id=proj,
        kind="sources",
        ext="mp4",
    )
    assert key.startswith(f"ws/{ws}/sources/{proj}/")
    assert key.endswith(".mp4")
    assert ".." not in key
    assert "/" not in key.rsplit("/", 1)[-1].rstrip(".mp4")


def test_build_key_sanitises_kind_and_extension():
    ws, proj = uuid.uuid4(), uuid.uuid4()
    key = build_key(
        workspace_id=ws,
        project_id=proj,
        kind="../../etc",
        ext=".m/../../p4",
    )
    assert ".." not in key
    # Only alphanumerics survive the extension sanitiser.
    assert key.endswith(".mp4") or key.endswith(".")


def test_keys_are_unique_per_call():
    ws, proj = uuid.uuid4(), uuid.uuid4()
    keys = {
        build_key(workspace_id=ws, project_id=proj, kind="sources", ext="mp4") for _ in range(50)
    }
    assert len(keys) == 50


# --------------------------------------------------------------------- cursors


def test_cursor_round_trip_preserves_microseconds():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 26, 15, 4, 5, 123456, tzinfo=UTC)
    assert _decode_cursor(_encode_cursor(now)) == now


def test_cursor_is_url_safe():
    from datetime import UTC, datetime

    # `+` in a raw ISO timestamp became a space in a query string — the original bug.
    encoded = _encode_cursor(datetime.now(UTC))
    assert not any(ch in encoded for ch in "+/=")


def test_malformed_cursor_is_a_conflict_not_a_500():
    for bad in ["not-a-cursor", "!!!", ""]:
        with pytest.raises(ConflictError) as exc:
            _decode_cursor(bad)
        assert exc.value.code == "invalid_cursor"

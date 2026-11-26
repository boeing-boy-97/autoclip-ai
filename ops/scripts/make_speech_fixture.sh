#!/usr/bin/env bash
# =====================================================================================
# Generate a media fixture that contains REAL SPEECH.
#
# Why this exists: the Phase 0 fixtures are synthetic test patterns with sine tones. They are
# perfect for testing media handling, but they contain no speech — so they can prove that
# transcription *runs*, never that it is *correct*. Pipeline tests need audio with known words in
# it, otherwise a broken ASR integration looks identical to a working one.
#
# How: a real TTS voice (Piper, MIT licence, runs locally) speaks a fixed script; FFmpeg muxes
# that audio with a synthetic video track. The transcript is therefore verifiable word-for-word
# against the script below.
#
# Usage: ops/scripts/make_speech_fixture.sh [--force]
# Output: backend/tests/fixtures/generated/real_speech.mp4  (gitignored)
#
# Requirements: ffmpeg, python3, and `pip install piper-tts`. The voice model (~63 MB) is cached
# in ./models/tts and is NOT committed.
# =====================================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${REPO_ROOT}/backend/tests/fixtures/generated"
MODEL_DIR="${REPO_ROOT}/models/tts"
VOICE="en_US-lessac-medium"
TARGET="${OUT}/real_speech.mp4"

if [[ -f "${TARGET}" && "${1:-}" != "--force" ]]; then
  echo "==> ${TARGET} already exists (use --force to regenerate)"
  exit 0
fi

mkdir -p "${OUT}" "${MODEL_DIR}"

# The script the speaker reads, and the ground truth the tests assert against.
SCRIPT="$(cat <<'EOS'
Most people think learning to code is about memorising syntax. It is not.
The real skill is breaking a problem into pieces small enough to test.
When I started, I spent three weeks copying examples and understood nothing.
The day it clicked was the day I deleted everything and rebuilt it from scratch.
Start with one small project you actually care about, and finish it.
EOS
)"

echo "==> ensuring TTS voice ${VOICE} is available"
python3 - <<PY
from pathlib import Path
try:
    from piper.download_voices import download_voice
except ImportError as exc:
    raise SystemExit(
        "piper-tts is not installed. Install it with: pip install piper-tts"
    ) from exc
target = Path("${MODEL_DIR}") / "${VOICE}.onnx"
if not target.exists():
    download_voice("${VOICE}", Path("${MODEL_DIR}"))
print("voice ready:", target.exists())
PY

RAW="${OUT}/.speech_raw.wav"
echo "==> synthesising speech"
printf '%s' "${SCRIPT}" > "${OUT}/.speech_script.txt"
piper -m "${MODEL_DIR}/${VOICE}.onnx" -f "${RAW}" -i "${OUT}/.speech_script.txt" 2>/dev/null

DURATION="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${RAW}")"
echo "==> speech duration: ${DURATION}s"

echo "==> muxing into an MP4 (synthetic video track + real speech audio)"
ffmpeg -y -v error \
  -f lavfi -i "testsrc2=size=640x360:rate=24:duration=${DURATION}" \
  -i "${RAW}" \
  -c:v libx264 -preset veryfast -crf 32 -pix_fmt yuv420p \
  -c:a aac -b:a 96k -shortest \
  "${TARGET}"

rm -f "${RAW}" "${OUT}/.speech_script.txt"

# Verify what we produced rather than assuming.
PROBED="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${TARGET}")"
STREAMS="$(ffprobe -v error -show_entries stream=codec_type -of csv=p=0 "${TARGET}" | tr '\n' ',' )"
echo "==> ${TARGET}"
echo "    duration: ${PROBED}s   streams: ${STREAMS}"
if [[ "${STREAMS}" != *"video"* || "${STREAMS}" != *"audio"* ]]; then
  echo "ERROR: fixture must contain both a video and an audio stream" >&2
  exit 1
fi
echo "==> speech fixture ready"

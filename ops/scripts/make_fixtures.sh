#!/usr/bin/env bash
# =====================================================================================
# Generate deterministic test media with FFmpeg. No third-party media is used.
# Output: backend/tests/fixtures/generated/  (gitignored — regenerate on demand)
#
# Usage: ops/scripts/make_fixtures.sh [--force]
# =====================================================================================
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${REPO_ROOT}/backend/tests/fixtures/generated"
FFMPEG="${FFMPEG_BIN:-ffmpeg}"
FFPROBE="${FFPROBE_BIN:-ffprobe}"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

command -v "${FFMPEG}" >/dev/null || { echo "!! ffmpeg not found" >&2; exit 1; }
command -v "${FFPROBE}" >/dev/null || { echo "!! ffprobe not found" >&2; exit 1; }

mkdir -p "${OUT}"

# make <name> <args...> — idempotent unless --force
make() {
  local name="$1"; shift
  local target="${OUT}/${name}"
  if [[ -f "${target}" && ${FORCE} -eq 0 ]]; then
    echo "  = ${name} (exists)"
    return 0
  fi
  if "${FFMPEG}" -hide_banner -loglevel error -y "$@" "${target}"; then
    local size
    size=$(stat -c%s "${target}" 2>/dev/null || stat -f%z "${target}")
    echo "  + ${name} (${size} bytes)"
  else
    echo "  !! failed: ${name}" >&2
    return 1
  fi
}

echo "==> generating fixtures in ${OUT}"

# 1. Baseline 30 s talking-head stand-in: moving pattern + tone. Fast smoke-test render.
make talking_30s.mp4 \
  -f lavfi -i "testsrc2=size=640x360:rate=24:duration=30" \
  -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=30" \
  -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -c:a aac -shortest

# 2. Two-speaker stand-in: left/right panels alternate (exercises split-screen + speaker logic).
make two_speakers_90s.mp4 \
  -f lavfi -i "color=c=0x1f3a93:size=320x360:rate=24:duration=90" \
  -f lavfi -i "color=c=0x933a1f:size=320x360:rate=24:duration=90" \
  -f lavfi -i "sine=frequency=180:sample_rate=48000:duration=90" \
  -filter_complex "[0:v][1:v]hstack=inputs=2[v]" \
  -map "[v]" -map 2:a -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -c:a aac -shortest

# 3. Black-frame inserts (QA black-frame detection must fail this).
make black_frames.mp4 \
  -f lavfi -i "testsrc2=size=640x360:rate=24:duration=10" \
  -f lavfi -i "color=c=black:size=640x360:rate=24:duration=2" \
  -f lavfi -i "testsrc2=size=640x360:rate=24:duration=8" \
  -f lavfi -i "sine=frequency=300:sample_rate=48000:duration=20" \
  -filter_complex "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]" \
  -map "[v]" -map 3:a -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -c:a aac -shortest

# 4. Frozen video segment (QA freeze detection must fail this).
make frozen.mp4 \
  -f lavfi -i "testsrc2=size=640x360:rate=24:duration=10" \
  -f lavfi -i "sine=frequency=300:sample_rate=48000:duration=10" \
  -vf "loop=loop=72:size=1:start=0" \
  -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -c:a aac -shortest

# 5. Silent audio track (QA audio-level / speech checks).
make silent.mp4 \
  -f lavfi -i "testsrc2=size=640x360:rate=24:duration=10" \
  -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
  -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -c:a aac -t 10

# 6. Square aspect (QA aspect-ratio check).
make square.mp4 \
  -f lavfi -i "testsrc2=size=360x360:rate=24:duration=10" \
  -f lavfi -i "sine=frequency=250:sample_rate=48000:duration=10" \
  -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -c:a aac -shortest

# 7. Corrupt file: first 2 KB of the baseline (upload validation must reject it).
if [[ ! -f "${OUT}/corrupt.mp4" || ${FORCE} -eq 1 ]]; then
  make talking_30s.mp4 -f lavfi -i "testsrc2=size=320x240:rate=10:duration=2" \
    -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p
  head -c 2048 "${OUT}/talking_30s.mp4" > "${OUT}/corrupt.mp4"
  echo "  + corrupt.mp4 (truncated)"
fi

# 8. Long mid-clip silence (silence trimming must shorten it).
make long_silence.mp4 \
  -f lavfi -i "testsrc2=size=640x360:rate=24:duration=20" \
  -f lavfi -i "sine=frequency=300:sample_rate=48000:duration=6" \
  -f lavfi -i "anullsrc=channel_layout=mono:sample_rate=48000:duration=8" \
  -f lavfi -i "sine=frequency=300:sample_rate=48000:duration=6" \
  -filter_complex "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]" \
  -map 0:v -map "[a]" -c:v libx264 -preset veryfast -crf 34 -pix_fmt yuv420p -c:a aac -shortest

# 9. Speech-ish WAV for ASR plumbing (tonal, not speech: ASR correctness is not asserted here).
make speech_10s.wav \
  -f lavfi -i "sine=frequency=440:sample_rate=16000:duration=10" -ac 1

# ---------------------------------------------------------------- verification
echo "==> probing every fixture"
FAIL=0
for f in "${OUT}"/*; do
  name="$(basename "${f}")"
  if [[ "${name}" == "corrupt.mp4" ]]; then
    if "${FFPROBE}" -v error -show_entries format=duration -of csv=p=0 "${f}" >/dev/null 2>&1; then
      echo "  ✗ corrupt.mp4 unexpectedly probed clean"; FAIL=1
    else
      echo "  ✓ corrupt.mp4 correctly rejected by ffprobe"
    fi
    continue
  fi
  if [[ "${name}" == *.wav ]]; then
    # Audio-only fixture: assert an audio stream, not a video one.
    a=$("${FFPROBE}" -v error -select_streams a:0 \
          -show_entries stream=codec_name,channels,sample_rate -of csv=p=0 "${f}" 2>/dev/null || true)
    if [[ -n "${a}" ]]; then echo "  ✓ ${name} → audio ${a}"; else echo "  ✗ ${name} unreadable"; FAIL=1; fi
    continue
  fi
  info=$("${FFPROBE}" -v error -select_streams v:0 \
           -show_entries stream=width,height,duration -of csv=p=0:s=x "${f}" 2>/dev/null || true)
  if [[ -n "${info}" ]]; then
    echo "  ✓ ${name} → ${info}"
  else
    echo "  ✗ ${name} has no readable video stream"; FAIL=1
  fi
done

echo "==> fixtures ready in ${OUT}"
[[ ${FAIL} -eq 0 ]] || { echo "!! fixture verification failed" >&2; exit 1; }

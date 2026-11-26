# Testing strategy

## 0. Principles

* **No mock in production paths.** A test asserts that `app/` contains no `if testing`,
  `if settings.environment == "test"`, or hardcoded fake-provider branches. External services are
  mocked at the HTTP boundary with `respx` (provider APIs) or by recorded cassettes (Google APIs),
  or replaced by a real local service (Postgres/Redis/MinIO) — never by an in-app switch.
* **Real media in integration tests.** Audio/video fixtures are generated deterministically by FFmpeg
  (`ops/scripts/make_fixtures.sh`), so tests exercise real `ffprobe`/`ffmpeg`/OpenCV behaviour.
* **Assert the negative.** Most of this product's value is in *refusing* to do the wrong thing:
  publishing an un-QA'd clip, charging twice, cutting a word in half, mixing workspaces. Those get
  explicit tests.
* Tests are the acceptance evidence for each phase; a phase is not "done" on green wishes.

## 1. Layers

| Layer | Location | Runs against | Budget | Gate |
|---|---|---|---|---|
| Unit | `backend/tests/unit` | pure functions, no I/O | < 30 s total | must be 100 % green, no network |
| Integration | `backend/tests/integration` | Postgres, Redis, MinIO, FFmpeg (compose/testcontainers) | < 10 min | green on every PR |
| Contract | `backend/tests/contract` | `docs/openapi.json` vs live app | < 30 s | fails on drift |
| E2E | `frontend/tests/e2e` (Playwright) | full compose stack | < 15 min | green before release |
| Load / soak | `ops/scripts/loadtest.py` | staging | manual | 8 | 
| Security | `ops/scripts/security_check.sh` | repo + images | manual/CI nightly | 8 |

## 2. Unit coverage targets (the logic that must never silently break)

1. **Intervals & timestamps** — half-open maths, clamping to duration, min-length, snapping to word
   boundaries, "never cut inside a word" (property-based with Hypothesis over random transcripts).
2. **Stage graph** — every legal transition allowed, every illegal one rejected; no state reachable
   by two different legal paths with different meanings; terminal states absorbing.
3. **Scoring** — weights sum to 1.0 (config validation), each of the four sub-scores computed from the
   documented rollup, bounds respected, `weights_version` recorded, missing criterion handling.
4. **Duration optimisation** — a 38 s complete idea is preserved rather than clamped to 30 s; presets
   are preferences; overrun beyond hard max rejected with a reason.
5. **Diversity** — IoU suppression, semantic dedupe threshold, topic/speaker quota, intentional
   overlap allowance, determinism for a fixed input (stable ordering, no dict-order dependence).
6. **Caption timing validator** — too early, too late, on-screen too long, too many words, out of
   safe area, overlapping cues, zero-length cues.
7. **Metadata validation** — title length limits, hashtag count/format, no fabricated claims
   (forbidden-claim regex list), language consistency, description non-empty.
8. **Publish state machine** — legal paths only; `published` unreachable without a recorded
   `youtube_video_id`; duplicate guards; retry does not create a second job.
9. **Security helpers** — YouTube URL allowlist (SSRF), path-safety (no user strings in paths),
   FFmpeg filter/subtitle escaping (quotes, newlines, `:` `;` `[` `]` `,` injection attempts).
10. **Schema validators** — AI payload validation rejects out-of-range timestamps, unknown enum
    members, extra keys; the repair prompt is generated with the actual error text.
11. **Crypto** — AES-GCM encrypt/decrypt round-trip, key-version rotation, tamper detection;
    Argon2 verify/reject.
12. **Budget** — warn at 80 %, hard stop at 100 %, never mid-render if the check can be done up front.

## 3. Integration coverage

* Migrations: `alembic upgrade head` then `downgrade base` then up again, on a clean database; and a
  drift check that the migrated schema matches `docs/database/schema.sql`.
* Workspace isolation: a repository-level matrix test that attempts every read/write across two
  workspaces and asserts `404`/zero rows — not `403`, no leakage.
* Storage: put/get/sign/expire; signed URL cannot access another workspace's key; lifecycle cleanup
  removes only expired derived assets.
* FFmpeg: render a 15 s fixture to 1080×1920; probe the output; assert duration within ±150 ms, both
  streams present, no black frames; assert captions are burned (frame pixel diff vs uncaptioned).
* ASR: `faster-whisper` tiny model on a short generated speech clip — asserts word timestamps are
  monotonic and inside the clip range (quality is not asserted; plumbing is).
* QA: four deliberately-broken fixtures (black frames, silent audio, wrong aspect, frozen video)
  each fail exactly the expected blocking checks and pass the others.
* Repair: a crop failure triggers `recrop`, produces version 2, and QA passes; attempts are bounded.
* YouTube client: resumable upload against a cassette — including a mid-upload 503 that resumes from
  the committed offset (asserts no re-send from byte 0), and a post-upload failure that recovers the
  recorded video id instead of re-uploading.
* Jobs: eager-mode task contract — idempotency (same fingerprint twice → one execution), retry
  backoff schedule, dead-letter after max attempts, cancellation, stuck-job sweeper.

## 4. E2E (Playwright)

Register → create workspace → upload fixture with rights declaration → watch SSE progress → analysis
rendered → clips generated → open editor → change caption style → re-render → QA report visible →
approve → publish (Google API intercepted; asserted request shape) → history shows the video id →
dashboard numbers match the database. Each step asserts real UI state, not just presence of elements.

## 5. Fixtures (`ops/scripts/make_fixtures.sh`)

Generated into `backend/tests/fixtures/generated/` (gitignored; regenerated on demand):

| Fixture | How | Used for |
|---|---|---|
| `talking_30s.mp4` | `testsrc2` + `sine` 1080p30 | fast smoke render |
| `two_speakers_90s.mp4` | two colour panels + tone alternation | reframe/split-screen |
| `speech_10s.wav` | locally synthesized speech (espeak if available, else tone) | ASR plumbing |
| `black_frames.mp4` | 2 s black inserts | QA black-frame check |
| `frozen.mp4` | repeated frame for 3 s | QA freeze check |
| `silent.mp4` | video with silent audio track | QA audio check |
| `square.mp4` | 720×720 | aspect-ratio QA |
| `corrupt.mp4` | truncated file | upload rejection |
| `long_silence.mp4` | 8 s of silence mid-clip | silence trimming |

Fixtures never include third-party copyrighted media.

## 6. CI (`/.github/workflows/ci.yml`)

Stages: lint (`ruff format --check`, `ruff check`, `mypy app/`) → unit → integration (services:
postgres, redis, minio) → schema verify → openapi validate (and drift vs app export) → frontend
(`tsc --noEmit`, `eslint`, `vitest`, `next build`) → E2E on the composed stack → security
(`bandit`, `pip-audit`, `npm audit --omit=dev`) → coverage gates.

Coverage gates: **85 %** for `app/intelligence`, `app/production`, `app/qa`, `app/publishing`;
**70 %** overall backend. Frontend: 60 % on `src/lib` (business logic), no gate on pure presentational
components.

## 7. What is explicitly NOT tested automatically

Real YouTube uploads and real paid AI calls — they require operator credentials. Their code paths are
covered by contract tests and cassettes, and the app reports missing configuration explicitly
(`youtube_not_configured`, `provider_not_configured`). Claiming otherwise would be the fake
integration the spec forbids.

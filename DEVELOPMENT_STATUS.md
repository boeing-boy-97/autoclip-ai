# DEVELOPMENT STATUS

**Product:** AutoClipper AI — autonomous video intelligence, clipping & YouTube publishing
**Current phase:** Phase 2 — Transcription and analysis pipeline — **COMPLETE**
**Previous phases:** Phase 1 — Auth, projects, source ingestion — **COMPLETE** ·
Phase 0 — Architecture and project foundation — **COMPLETE**
**Date:** 2026-11-26
**Verification commands:**
`./ops/scripts/phase0_verify.sh` → **20 checks passed, 0 failed**
`./ops/scripts/verify_migrations.sh` → **NO DRIFT (1039 = 1039 DDL lines)**
`pytest` → **182 passed** (82 unit + 100 integration, real PostgreSQL/Redis/FFmpeg/Whisper)
`ruff check .` → **All checks passed**
`python3 ops/scripts/validate_openapi.py` → **62 operations / 53 paths / 43 schemas — passed**

---

## 1. Phase 0 deliverables — status

| Deliverable | Status | Artifact |
|---|---|---|
| Architecture document | ✅ Done | `docs/ARCHITECTURE.md` (35 KB) |
| Architecture diagrams | ✅ Done | `docs/diagrams/README.md` (7 Mermaid diagrams) |
| Database schema | ✅ Done, **verified against live PostgreSQL 17** | `docs/database/schema.sql` |
| Schema invariant proof | ✅ Done, 16 invariants pass | `docs/database/verify_invariants.sql` |
| API specification | ✅ Done, **valid OpenAPI 3.1** | `docs/openapi.json` (62 ops, 53 paths, 43 schemas) |
| Environment configuration | ✅ Done, **all 64 settings documented** | `.env.example` |
| Docker development environment | ✅ Done, structure validated | `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile` |
| Logging strategy | ✅ Done + implemented | `docs/LOGGING.md`, `backend/app/core/logging.py` |
| Testing strategy | ✅ Done | `docs/TESTING.md` |
| Security/compliance design | ✅ Done | `docs/SECURITY.md` |
| Deployment/ops runbook | ✅ Done | `docs/RUNBOOK.md` |
| Project directory structure | ✅ Done | see §3 |
| Config: scoring / analysis / pricing | ✅ Done, validated | `config/*.yaml` |
| Editing templates | ✅ Done, 8 styles validated | `config/templates/*.yaml` |
| CI pipeline definition | ✅ Done | `.github/workflows/ci.yml` |
| Minimal runnable backend scaffold | ✅ Done, **boots and tested** | `backend/app/` |
| Backend tests | ✅ Done, **43 passing** | `backend/tests/unit/` |
| Test media fixtures | ✅ Done, **9 fixtures generate and probe correctly** | `ops/scripts/make_fixtures.sh` |
| Lint / format clean | ✅ Done, **ruff check + format pass** | `ruff.toml` |

---

## 2. What was actually verified (not merely written)

Every claim below was executed in this workspace:

| Verification | Result |
|---|---|
| `schema.sql` applies to real PostgreSQL 17.12 + `citext` + `pgvector` | **No errors.** 33 tables, 84 indexes, 81 FKs, 23 CHECKs, 10 triggers, 16 enums |
| 16 schema invariants (dedupe/duplicate-publish/interval/score bounds) | **16/16 PASS** |
| `docs/openapi.json` validates as OpenAPI 3.1 | **Valid**; 62 ops, unique operationIds, all problem+json, all $refs resolve, all path params declared |
| Scoring weights sum to exactly 1.0; rollup references only declared criteria | **Pass** |
| 8 editing templates parse, all 1080×1920 with required sections | **Pass** |
| `docker-compose.yml` parses; 14 services, 7 queues + beat + infra | **Pass** |
| All 64 `Settings` fields documented in `.env.example` | **Pass** |
| Backend boots under uvicorn; `/health` reports live DB, Redis, storage, FFmpeg | **Pass** |
| Settings fail-fast: names every missing field; rejects bad base64, wrong key length, weak pepper, placeholders (incl. base64-wrapped), local storage in prod, eager Celery in prod | **Pass** |
| Error contract: 404/405/422 all return `application/problem+json` with stable `code`; trace ids generated and echoed | **Pass** |
| Log redaction: secrets, signed-URL signatures, emails | **Pass** |
| Unit test suite (43 tests) | **All passed** |
| `ruff check` + `ruff format --check` on `app/` and `tests/` | **Clean** |
| 9 FFmpeg-generated media fixtures (incl. black-frame, frozen, silent, square, corrupt) | **All generate and probe as intended**; corrupt file correctly rejected by ffprobe |
| Total workspace size | 5.8 MB (fixtures 4.7 MB) |

Bugs found *by* verification and fixed (recorded because they show the checks have teeth):
1. `clips` composite FK referenced a unique constraint that didn't exist yet → schema wouldn't apply.
2. Module-level `settings = get_settings()` broke testability and hid config errors → replaced with explicit accessor.
3. FastAPI's default 404 returned `{"detail": ...}` as `application/json`, violating the documented error contract.
4. Placeholder detection inspected the base64 string, where markers are invisible → now inspects decoded bytes.
5. Trace id fell back to `"unavailable"` because exception handlers couldn't see the middleware's id.
6. Verification script's own error detection was inverted (reported success on failure).
7. Fixture probe required a video stream from an audio-only WAV, and initial fixtures totalled 93 MB —
   regenerated at 640×360/CRF 34 to 4.7 MB, since fixtures are structural test inputs.
8. Verification steps ran from `backend/` after a `cd`, so a repo-relative script path broke; the
   script now returns to the repo root explicitly.

---

## 3. Project structure

```
autoclip-ai/
├── DEVELOPMENT_STATUS.md          # this file
├── README.md
├── docker-compose.yml             # 14 services: api, web, 7 workers, beat, pg, redis, minio
├── .env.example                   # 64 documented settings
├── .github/workflows/ci.yml       # lint → unit → integration → contract → e2e → security
├── config/
│   ├── scoring.yaml               # weights, rollup, auto-approve gates, diversity thresholds
│   ├── analysis.yaml              # salience sampling, ASR, scenes, moment detection
│   ├── pricing.yaml               # cost model + YouTube quota units
│   └── templates/*.yaml           # 8 editing templates (data, not code)
├── docs/
│   ├── ARCHITECTURE.md            # authoritative design
│   ├── diagrams/README.md         # 7 Mermaid diagrams
│   ├── openapi.json               # generated + validated contract
│   ├── LOGGING.md, TESTING.md, SECURITY.md, RUNBOOK.md
│   └── database/
│       ├── schema.sql             # full DDL, verified
│       └── verify_invariants.sql  # 16 executable invariant proofs
├── backend/
│   ├── Dockerfile                 # dev + prod targets, FFmpeg/OpenCV runtime
│   ├── requirements*.txt
│   ├── pytest.ini
│   ├── app/
│   │   ├── main.py                # app factory, problem+json, health
│   │   ├── core/config.py         # fail-fast settings
│   │   ├── core/logging.py        # structlog + redaction
│   │   ├── db/{models,migrations}/  (empty — Phase 1)
│   │   ├── api/v1/                  (empty — Phase 1)
│   │   ├── services/, intelligence/, production/, qa/, publishing/, workers/, providers/
│   │   │                            (empty — Phases 1–7, mapped in ARCHITECTURE §14)
│   └── tests/{unit,integration,e2e}/  unit populated
├── frontend/                      # structure only — Phase 1
└── ops/
    ├── scripts/verify_schema.sh   # reproducible schema + invariant check
    ├── scripts/validate_openapi.py
    ├── scripts/gen_openapi.py     # replaced by app export in Phase 1
    └── scripts/phase0_verify.sh   # all Phase 0 checks
```

---

## 4. Key design decisions and why

1. **Integer milliseconds, half-open intervals, enforced twice** (Pydantic + DB CHECK). Float seconds
   produce off-by-one drift at clip boundaries; the DB constraint means a buggy code path can't persist
   an invalid interval.
2. **Workspace scoping by construction.** Repository functions require `workspace_id`; there is no
   fetch-by-id helper, so cross-tenant access is a type error rather than a review miss. Cross-workspace
   access returns 404, not 403.
3. **Duplicate publishing is prevented in the database**, via a partial unique index on
   `(clip_id, channel_id) WHERE state IN (live states)`, plus a unique idempotency key and persisting
   `youtube_video_id` *before* any retry decision.
4. **Decomposed scoring.** Four sub-scores (`content_potential`, `technical_quality`,
   `context_completeness`, `audience_relevance`) are stored and displayed; `overall` is presentational.
   Auto-approval is off by default and gated on QA pass + context completeness.
5. **Frame salience sampling** keeps vision cost O(1) in video length (≤ 48 frames regardless of runtime).
6. **Rendering is declarative** (`RenderPlan` JSON → FFmpeg filtergraph), and **QA re-inspects the
   file**, never trusting the plan. Nothing failing a blocking check is publishable (`publishable`
   derives from the latest passing report).
7. **SQLite was never used**, even for convenience: the duplicate-publish and partial-index guarantees
   are Postgres-specific and central to the product's integrity story.
8. **Missing integrations are reported, not simulated.** `/health` returns
   `configured: false` with the exact env vars needed. No code path returns fake success.

---

---

## 4A. Phase 1 — Authentication, projects and source ingestion (COMPLETE)

### What was built

| Area | Delivered | Files |
|---|---|---|
| Security primitives | Argon2id (t=3/m=64 MiB/p=4) + HMAC-SHA256 pepper, opaque 256-bit session tokens (SHA-256 stored), CSRF double-submit digests, AES-256-GCM `SecretBox`, login throttling in Redis | `app/core/security.py` |
| State machine | 24-stage declarative graph, `assert_transition`, `resume_stage`, `progress_percent`; `FAILED`/`CANCELLED` absorbing | `app/core/stage_graph.py` |
| Persistence | All 33 domain tables mapped; engine/session factories; **one transaction per request** committed by the dependency | `app/db/session.py`, `app/db/models/__init__.py` |
| Migrations | `0001_baseline` reproduces `schema.sql` exactly (drift-checked, downgrade round-trip) | `app/db/migrations/` |
| Data access | Every repository call requires `workspace_id`; opt-in keyset pagination | `app/db/repositories.py` |
| Auth API | `POST /auth/register`, `/auth/login`, `/auth/logout`, `GET /auth/me` | `app/api/v1/auth.py` |
| Projects API | CRUD + `/stages` journal; soft delete; cursor pagination; settings-only PATCH | `app/api/v1/projects.py` |
| Workspaces/system API | `GET /workspaces`, `/dashboard`, `/jobs`, `/jobs/{id}` | `app/api/v1/system.py` |
| **Source ingestion** | Multipart upload → extension/MIME/magic-byte checks → streamed storage with size cap + SHA-256 → real `ffprobe` inspection → facts persisted → stage journal advanced | `app/api/v1/sources.py`, `app/services/ingest.py` |
| Storage abstraction | Local filesystem (dev, refuses production) and S3-compatible; server-generated keys only; signed URLs; no media streamed by the API | `app/services/storage.py` |
| Media validation | Container sniffing, FFmpeg probing with `-protocol_whitelist file` (SSRF guard), timeout, duration/resolution/audio/fps rules | `app/services/media_probe.py` |
| Error contract | `problem+json` for `ApiError`, repository `NotFoundError`/`ConflictError`, framework `HTTPException`, validation errors; `Retry-After` preserved | `app/main.py` |
| Tests | 130 passing: 72 unit, 58 integration against real PostgreSQL 17 + Redis + FFmpeg | `tests/unit/`, `tests/integration/` |

### Live end-to-end evidence (uvicorn, real PostgreSQL, real FFmpeg)

| Step | Observed |
|---|---|
| Register | `201` → user + workspace + `owner` membership + `csrf_token` |
| Upload 30 s talking-head MP4 | `201`, `state=ready`, `duration_ms=30000`, `640x360`, `h264`/`aac`, `byte_size=1202759` — all matching `ffprobe` |
| Stored object integrity | `stored size 1202759`; SHA-256 of stored object **identical** to the upload |
| Stage journal | `None→CREATED`, `CREATED→SOURCE_VALIDATION`, `→INGESTING`, `→MEDIA_INSPECTION` |
| Corrupt / renamed / audio-only file | `415 unsupported_media`, audited, project left `CREATED` and retryable |
| Missing rights declaration | `422 source_not_authorized` |
| Upload without CSRF | `403 forbidden` |
| Second source on one project | `409 project_already_has_source` |
| Non-member upload (foreign workspace) | `404 workspace_not_found` |
| Non-member upload (own workspace, foreign project id) | `404 project_not_found` |
| Unauthenticated upload | `401` |
| Cross-tenant dashboard/job probe | `404`, and the response no longer echoes the requested id |

### Bugs found by verification and fixed in this phase

1. **Silent rollback:** `get_db` never committed, so an upload returned `201` while every write was discarded at session close. Fixed centrally: the dependency owns the transaction boundary.
2. **File corruption on upload:** the magic-byte sniff consumed the first 4 KiB of the spool handle and the copy then streamed from the same position — stored files were missing their header. A test comparing stored bytes to the uploaded bytes caught it; the sniff now uses its own handle.
3. **`ProjectOut.source` serialisation crash:** validating the ORM object embedded raw `sha256` bytes → `500` as soon as a project had a source. Now built through `SourceOut.from_model`.
4. **Dependency errors escaped as `500`:** `NotFoundError` raised by the workspace dependency reached the generic handler. Added dedicated handlers → `404`/`409` with the documented codes.
5. **Invalid `inet` cast:** `client.host` of `testclient` (or a proxy header) broke `audit_logs.ip`. Added `valid_ip()` validation at every write site.
6. **Cursor corruption:** ISO-8601 cursors travelled through the query string, where `+00:00` became a space → `409 invalid_cursor` on page 2. Cursors are now URL-safe base64 of epoch microseconds.
7. **`Retry-After` dropped:** the `ApiError` handler built a fresh response and lost headers, breaking 429 semantics.
8. **Test-infrastructure flaws that hid real behaviour:** actors shared one cookie jar (so "cross-tenant" tests compared a user with itself), and fixture ordering let Redis flushes invalidate live sessions. Both fixed; isolation tests now use independent clients.
9. **Phantom dedupe path:** a `find_by_checksum` branch was unreachable (a project holds one source, so the 409 always fires first) — removed rather than left as dead code.
10. Mapped attribute named `bytes` shadowed the builtin and broke every later annotation, masked by a misleading `SADeprecationWarning` under pytest.

### Deliberate design decisions

* **Rejected uploads do not fail the project.** `FAILED` is absorbing, and one bad file must not force the user to recreate a project; the refusal is audited (`source.rejected`) and no source row is written, so the retry path stays open.
* **A project holds exactly one source.** Enforced by the schema (unique index) and reported as `409`, rather than a soft "latest wins" rule.
* **Progress is `null` where no honest denominator exists** — no invented percentages.
* **Local storage cannot mint signed URLs**; it raises instead of returning a plausible-looking fake.

### New environment variables

None. Phase 1 uses settings already documented in `.env.example`
(`MAX_UPLOAD_MB`, `STORAGE_BACKEND`, `LOCAL_STORAGE_ROOT`, `S3_*`, `FFPROBE_BIN`,
`MAX_SOURCE_DURATION_MINUTES`).

### Migration explanation

`0001_baseline` embeds the reviewed DDL from `docs/database/schema.sql` and is verified by
`ops/scripts/verify_migrations.sh`: apply `schema.sql` to a reference database, apply migrations to
another, compare normalised DDL (**1039 vs 1039 lines, no drift**), then `downgrade base` →
`upgrade head` to prove reversibility. Model changes in this phase (PG `Enum` declarations,
`ARRAY(Integer)`, the `byte_size`→`bytes` mapping) are ORM-level only and produce no DDL change.

### Phase 1 exit criteria — status

| Criterion | Status |
|---|---|
| Registration → login → session works with real cookies/CSRF | ✅ |
| Project created with legal initial stage and journal entry | ✅ |
| Real fixture upload inspected by `ffprobe`, facts recorded | ✅ |
| Corrupt / wrong-MIME / wrong-extension / audio-only rejected with specific codes | ✅ |
| Workspace isolation: non-member reads/writes are 404, never data, never 403 | ✅ |
| Dashboard numbers equal database counts | ✅ |
| Migrations reproduce the schema and reverse cleanly | ✅ |
| Frontend shell and auth UI | ❌ **Not started — see limitations** |

---

---

## 4B. Phase 2 — Transcription and analysis pipeline (COMPLETE)

### What was built

**Pipeline services** (`backend/app/services/`)
- `transcription.py` — real ASR behind a provider interface. `faster-whisper` on CPU with
  `tiny.en` int8 for local verification; the model dir, device, compute type and provider are all
  configuration, never hardcoded. Returns segments with word timings only where the model actually
  produced them — no interpolated or invented timestamps. Provider unavailability and provider
  failure are distinct exception types because they map to different HTTP outcomes (503 vs 422).
- `scoring.py` — ten content criteria plus four roll-ups loaded from `config/scoring.yaml`
  (`version 2026-01-v1`, weights validated to sum to 1.0). `overall` is computed as the weighted
  sum and recomputed in tests from the stored component scores, so the number is always traceable
  to its inputs. Scoring is labelled `method=heuristic` and carries an explicit
  "not a view prediction" disclaimer in the API response.
- `visual.py` — scene detection and frame metrics via FFmpeg alone (no OpenCV dependency added):
  brightness, contrast, sharpness, flat-frame ratio and a visual-quality score. Scene boundaries
  are verified against a fixture with cuts at exactly 10000 ms and 12000 ms.
- `analysis.py` — orchestrates audio extraction → transcription → visual analysis → semantic
  analysis → moment detection, persisting `media_assets`, `transcripts`,
  `transcript_segments`, `speakers`, `video_analysis`, `scenes`, `candidate_moments`,
  `candidate_scores` and a `processing_jobs` row per stage.

**API** — four new routes (20 paths total):
`POST /projects/{id}/analyze`, `GET /projects/{id}/analysis`, `GET /projects/{id}/transcript`
(`?include_words`), `GET /projects/{id}/moments` (`?limit&offset&selected_only&category`).

**Fixtures** — `ops/scripts/make_speech_fixture.sh` generates a 19.8 s clip with real speech
(piper TTS muxed with a `testsrc2` video track). The earlier tone fixtures contain no speech at
all, so they can only ever prove the honest-failure path; the speech fixture is what makes a
successful transcription testable.

**Spec export** — `python -m app.scripts.export_openapi` exports the *implemented* surface to
`docs/openapi-implemented.json`. See "Two contract files" below for why this is a separate file
from `docs/openapi.json`.

### Live end-to-end evidence

Fresh database, fresh storage root, real FFmpeg, real Whisper, real PostgreSQL:

```
1. upload: 201
2. analyze: 202
   status=MOMENT_DETECTION provider=faster-whisper model=tiny.en lang=en
   segments=5 words=65 candidates=5 selected=1 weights=2026-01-v1
3. transcript:
   5 segments, confidence 0.973, 65 words
   first: Most people think learning to code is about memorizing syntax.
   last : Start with one small project you actually care about and finish it.
4. top ranked moments:
   total=5 selected=1
   [ 7880-19680] other            overall= 66.0 (content 64 / tech 69 / context 78) sel=True
   [    0-11760] surprising_fact  overall= 63.2 (content 56 / tech 69 / context 84) sel=False
   [11980-19680] other            overall= 59.8 (content 59 / tech 69 / context 75) sel=False
5. cross-tenant isolation:
   transcript of foreign project: 404
   moments of foreign project:    404
```

The transcript text is genuinely what the model heard, not a canned string — the fixture script is
five sentences of natural speech and Whisper returned them with 0.973 confidence. The stage
journal is exact and ordered:
`CREATED → SOURCE_VALIDATION → INGESTING → MEDIA_INSPECTION → AUDIO_EXTRACTION → TRANSCRIPTION →
VISUAL_ANALYSIS → SEMANTIC_ANALYSIS → MOMENT_DETECTION`.

### Documented failure contract — all proven by tests, not asserted in prose

| Situation | Response | Side effects |
|---|---|---|
| Source has no speech | `422 no_speech_detected` | `transcripts` stays 0, job `FAILED` with `error_code=no_speech_detected`; project stays `MEDIA_INSPECTION` |
| Project has no source | `409 source_not_ready` | none |
| Transcript requested before analysis | `404 transcript_not_found` ("POST /analyze first") | none |
| Moments requested before analysis | `200` with `total=0` | none |
| Another user's project | `404` (not 403 — no existence leak) | none |
| Mutating request with bad CSRF | `403` | none |
| Anonymous request | `401` | none |

### Idempotency and re-run semantics

- Plain repeat `POST /analyze` reuses the existing transcript: no new transcript row, no new job.
  This is visible in the response, not just internal.
- `force=true` re-transcribes and *replaces* prior work — transcript, segments, speakers, scenes
  and `video_analysis` are deleted before reinsertion, in child-before-parent order.
- Re-running a completed analysis reports the existing work rather than failing a valid state.
  The stage graph is progress-only and acyclic, so `_enter()` *skips* stages already passed
  instead of attempting an illegal backward transition, and terminal stages are guarded.

### Bugs found by verification in this phase

Sixteen defects were found and fixed, most of which would have shipped silently:

1. Response shaping mutated ORM rows — `seg.words = None` in the GET handler was autoflushed and
   permanently erased stored word timings. Fixed by deciding at the schema boundary.
2. `force` was accepted in the query string and then ignored (returned 202 while doing an
   idempotent reuse). Flags must change behaviour or be rejected.
3. A forced re-run hit three `UNIQUE` violations in sequence — transcripts, speakers,
   `video_analysis` — because the replace path was written before checking the schema's
   constraints.
4. The job row was rolled back together with the failure it was supposed to record, because the
   failure path begins with `rollback()`.
5. `_enter()` walked the stage graph backwards on a re-run.
6. FFmpeg's `showinfo` log lines were invisible under `-v error`, so scene detection silently
   found nothing.
7. The ASR provider needed a path-decode workaround for the `av`/faster-whisper boundary;
   downgrading `av` was the wrong fix and was rejected.
8. Nine further lint/typing/consistency defects, including a public exception rename that broke
   92 tests through a single stale import site.

### Deliberate design decisions

- **Synchronous `/analyze`.** It returns 202 but does the work in-request. The docstring says so
  plainly: pretending to queue work that no worker will pick up would be a lie. The response shape
  is already the one the queued version will return, so switching to Celery will not change the
  contract.
- **Heuristic scoring is labelled as such**, with a disclaimer in the response body.
- **Honest 422 over a fabricated transcript.** A tone-only source is rejected
  (`no_speech_detected`) rather than given a plausible-looking transcript.
- **Two contract files.** `docs/openapi.json` is the Phase 0 *designed* product contract (62
  operations, all planned endpoints) and is enforced by `validate_openapi.py`. Overwriting it with
  today's 20 implemented paths would have deleted the roadmap and broken the validator — so the
  implemented surface exports separately to `docs/openapi-implemented.json`, kept honest by a test
  that fails if the committed file drifts from what the app serves. Diffing the two shows the
  remaining work.

### New environment variables

`ASR_PROVIDER`, `ASR_MODEL`, `ASR_DEVICE`, `ASR_COMPUTE_TYPE`, `ASR_MODEL_DIR`, `SCORING_CONFIG_PATH`,
`FFMPEG_BIN`, plus audio loudness targets. All documented in `.env.example`; all defaulted so the
app starts without them.

### Phase 2 exit criteria — status vs. the plan in §6

| Planned | Status |
|---|---|
| Audio extraction (16 kHz mono WAV) + loudness normalisation as `media_assets` | **Done** |
| Real ASR adapter persisting transcripts/segments/speakers with word timings | **Done** (word timings only where the model supplies them) |
| Candidate moments + component scores from `config/scoring.yaml`, explainable | **Done** — 10 criteria + 4 roll-ups, `overall` recomputed in tests |
| Scores presented as estimated content characteristics, never view predictions | **Done** — labelled `method=heuristic` with disclaimer |
| Ranked candidate list retrievable via API with scores traceable to transcript | **Done** |
| Tests: scoring determinism, idempotency, honest failures | **Done** — 182 passing |
| Celery worker + beat, retries with exponential backoff, resume after a killed worker | **Deferred** — the `processing_jobs` table, stage journal and statuses are in place; `/analyze` runs in-request and says so. Resumability is not claimed because it is not implemented. |
| Frontend: dashboard, create-project, upload progress, transcript viewer | **Deferred** — no frontend in this repo yet |

### Outstanding limitations (stated, not hidden)

1. **No Celery worker yet.** `POST /analyze` is synchronous. A long video will hold the request
   open for the duration of transcription. This is a real limitation, not a design nicety.
2. **`tiny.en` is a verification model, not a production model.** `large-v3` is not viable on
   this 2 vCPU / 1.9 GB host. Accuracy on real-world audio has not been measured here.
3. **Only English.** `language` is reported from the model, but no multilingual validation was done.
4. **`GET /jobs*` and `POST /jobs/{id}/cancel|retry` remain unimplemented** — they are in the
   designed contract (§`docs/openapi.json`) but not in the app.
5. **Heuristic scoring has not been validated against real viewership data.** It is a transparent,
   explainable starting point, deliberately not dressed up as a predictor.

## 5. Outstanding limitations (stated, not hidden)

### Expected at this phase
1. **No frontend yet.** The Next.js app is still scaffolding; the backend API is fully exercisable
   via HTTP (see the live evidence in §4A) but there is no UI. This is the largest remaining gap.
2. **Analysis, clip generation, rendering and publishing are not implemented.** They are specified
   in `docs/openapi.json` and blocked out in `config/*.yaml`, and will be Phase 2+.
3. **No background worker yet.** Ingest runs synchronously inside the request (media probing is
   CPU-bound but bounded); Celery wiring, job records and retries arrive with Phase 2, which is
   when the ~30 s probe should move off the request path.
4. **Timeline thumbnails, waveform peaks and the full 9:16 render preview** do not exist yet.

### External dependencies that cannot be demonstrated here
4. **YouTube publishing cannot be exercised** without operator-supplied OAuth client credentials
   (`YOUTUBE_CLIENT_ID`/`SECRET`). Code and cassette-based tests will exist in Phase 6; the app will
   report `youtube_not_configured` rather than pretend.
5. **Paid AI providers are not configured** in this environment. The pipeline will run in labelled
   heuristic mode (`heuristic_fallback`) — deliberately lower quality, never silently substituted.
6. **Items marked `TODO_VERIFY` in `config/pricing.yaml`** (YouTube quota unit costs, hosted STT and
   vision prices) must be confirmed against live provider documentation before Phase 6/7 rely on them.

### Known risks tracked in ARCHITECTURE §15
7. Diarization needs optional licensed models (degrades to heuristics).
8. Hinglish ASR quality may be insufficient; the caption engine will not "fix" meaning.
9. Render output is not bit-reproducible across hosts; QA compares properties + perceptual hashes.
10. `pgvector` is optional; semantic dedupe falls back to token-set cosine if absent.

### Environment notes
11. This sandbox has 2 vCPU / ~1.9 GB RAM / ~1 GB free on the storage root, so `MIN_FREE_DISK_GB=5`
    correctly reports storage as "below threshold" — the guard works, the host is just small.
12. Python 3.12 is the Docker target (wheel availability for `cv2`/`mediapipe`); local verification
    here ran on 3.13, which also works for the Phase 0 dependency subset.

---

## 6. The Phase 2 plan as written before the work (historical)

**Build:**
1. Celery worker + beat with Redis broker; `processing_jobs` rows created per stage, retries with
   exponential backoff, `job_id` on every stage transition, idempotent re-entry.
2. Audio extraction (16 kHz mono WAV) and FFmpeg loudness normalisation, stored as `media_assets`.
3. ASR adapter: real provider when `ASR_PROVIDER` credentials exist, otherwise the labelled
   `heuristic_fallback` path. Persist `transcripts`, `transcript_segments`, `speakers` with
   word-level timings where the provider supplies them.
4. Candidate-moment detection from transcript structure, `candidate_moments`/`candidate_scores`
   using `config/scoring.yaml`, with component scores stored and explainable.
5. Virality scores presented as **estimated content characteristics**, never as view predictions.
6. Frontend: dashboard, create-project flow, upload with progress, transcript viewer.
7. Tests: worker idempotency, resumability after a killed worker, scoring determinism.

**Exit criteria:** an uploaded fixture is transcribed, segmented, scored, and a ranked candidate
list is retrievable through the API with per-component scores traceable to the transcript; killing
the worker mid-stage resumes from the correct stage; no fabricated segment timings or scores
anywhere in the output.

---

## 7. Next phase — Phase 3: Rendering, the worker, and the frontend

**Build, in dependency order:**
1. **Celery worker + beat.** Redis broker is already configured and `processing_jobs` already
   records stage, attempts, retryability and `celery_task_id`. Convert `/analyze` to enqueue and
   return immediately with its existing response shape. Add exponential backoff and prove resume
   after a SIGKILL mid-stage — resumability is currently *not* claimed and must not be claimed
   until it is tested.
2. **`GET /jobs*`, `POST /jobs/{id}/cancel|retry`** — the only designed endpoints still missing.
3. **Clip rendering.** Cut, reframe to 9:16, burn captions from the stored word timings, persist
   `clips` + `clip_versions`. Captions must come from real timings; where the ASR gave none,
   the clip must not pretend it has them.
4. **Quality check** — reuse the Phase 2 visual metrics as the objective half of the QC report.
5. **YouTube OAuth and publish** — needs real credentials; until then the honest
   `youtube_not_configured` path stays.
6. **Frontend** — dashboard, create-project, upload with progress, transcript viewer, moment list
   with per-component scores visible.
7. **A production ASR model and a real accuracy measurement.** `tiny.en` proved the pipeline, not
   the product; word error rate on actual source material is unmeasured.

**Exit criteria:** a user can upload a video, watch progress update asynchronously, review a
ranked moment list with explainable scores, and receive a rendered 9:16 clip with correctly timed
captions — with every number traceable to real measured data and no fabricated output anywhere.

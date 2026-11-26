# DEVELOPMENT STATUS

**Product:** AutoClipper AI — autonomous video intelligence, clipping & YouTube publishing
**Current phase:** Phase 1 — Authentication, projects and source ingestion — **COMPLETE**
**Previous phase:** Phase 0 — Architecture and project foundation — **COMPLETE**
**Date:** 2026-11-26
**Verification commands:**
`./ops/scripts/phase0_verify.sh` → **20 checks passed, 0 failed**
`./ops/scripts/verify_migrations.sh` → **NO DRIFT (1039 = 1039 DDL lines)**
`pytest` → **130 passed** (72 unit + 58 integration, real PostgreSQL/Redis/FFmpeg)
`ruff check .` → **All checks passed**

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

## 6. Next phase — Phase 2: Transcription and analysis pipeline

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

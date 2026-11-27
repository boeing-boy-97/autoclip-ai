# DEVELOPMENT STATUS

**Product:** AutoClipper AI — autonomous video intelligence, clipping & YouTube publishing
**Current phase:** Phase 3 (partial) — Background worker, job control — **COMPLETE**
**Previous phases:** Phase 1 — Auth, projects, source ingestion — **COMPLETE** ·
Phase 0 — Architecture and project foundation — **COMPLETE**
**Date:** 2026-11-26
**Verification commands:**
`./ops/scripts/phase0_verify.sh` → **20 checks passed, 0 failed**
`./ops/scripts/verify_migrations.sh` → **NO DRIFT (1039 = 1039 DDL lines)**
`pytest` → **194 passed** (82 unit + 112 integration, real PostgreSQL/Redis/FFmpeg/Whisper)
`ruff check .` → **All checks passed**
`python3 ops/scripts/validate_openapi.py` → **62 operations / 53 paths / 43 schemas — passed**

---

## 0. Backend hardening pass (2026-11-27)

A review pass found the stack as committed could not run in containers and had three security
defects. All of the following are fixed on this branch, each with a regression test in
`backend/tests/unit/test_hardening.py`:

**Blocking**
1. `docker-compose.yml` started Celery with `-A app.workers.celery_app`; the package is `app.worker`.
   Every worker and beat crashed on start. Fixed, plus a CI step and a test that compare the compose
   file against the Celery app.
2. `frontend/Dockerfile` could not build: `output: "standalone"` was missing, `public/` did not exist,
   and the builder reused a `--omit=dev` install that lacks TypeScript. All three fixed.
3. `SCORING_CONFIG_PATH` was `../config/scoring.yaml` (cwd-relative) and resolved to `/config/...`
   inside the image, so `/analysis/plan` and every `analysis.run` returned 500. Now resolved against
   the repository root first (`Settings.scoring_config_file`).
4. `beat` ran with no schedule, so `analysis.sweep_stuck` never executed. `beat_schedule` added.
5. Seven queues were declared and only two routed; five workers could never receive work. Compose now
   runs exactly the queues that have tasks (`media`, `transcribe`, `analyze`) and
   `JobRepository.queue_for()` clamps anything else.

**Security**
6. Upload was gated on `_ws_dep("member")` — not a role, ranking 0 — so a **viewer** could upload.
   Now `editor`, and `min_role` is a `Literal`, so the mistake cannot recur.
7. CSRF could be skipped by adding any `Authorization: Bearer …` header to a cookie-authenticated
   request. The check now follows the credential that actually authenticated the request.
8. Rate limits were configured but never enforced. `app/core/ratelimit.py` (Redis fixed window,
   fail-open) now backs per-user limits on upload/analysis/render and per-IP limits on login/register;
   `LoginThrottle` no longer turns a Redis outage into a 500.
9. `SecretBox` could not rotate its key (it rejected any other `key_version`). It now holds a
   keyring: `SECRET_ENCRYPTION_KEY_VERSION` + `SECRET_ENCRYPTION_PREVIOUS_KEYS`.
10. `JobRepository.find_succeeded` was the only query without a `workspace_id` filter; the
    idempotency probe now requires it.

**Reliability**
11. `/health/livez` was used as the container healthcheck — always 200. Docker/compose now probe
    `/health/readyz`, which requires the database, Redis, storage **and FFmpeg**.
12. The readiness probe called blocking drivers from `async def` (event loop stall), `mkdir`-ed the
    storage root on every call, and leaked a Redis client. It is now sync (threadpool), side-effect
    free and closes what it opens.
13. Upload was `async def` around blocking spooling/FFprobe/S3 code; it is now a sync handler, and it
    spools to `TMP_ROOT` instead of the container's ephemeral `/tmp`.
14. `/health` no longer publishes `environment` + the integration inventory in production.
15. Real slow-query logging (`DB_SLOW_QUERY_MS`) replaces a listener that only ran under DEBUG.

**Developer experience**
16. `./ops/scripts/dev_env.sh` generates a working `.env` — `cp .env.example .env` always failed,
    because placeholder secrets are rejected at boot in every environment.
17. CI: coverage now measured over unit **and** integration suites (it was measured on unit tests
    only, where the routes and repositories have no coverage); `make_fixtures.sh || echo` and
    `pip-audit || true` no longer mask failures; `verify_migrations.sh` runs; ruff is pinned.

Verification in this environment: `ruff check` clean, `ruff format --check` clean over
`backend/app` + `backend/tests`, `pytest tests/unit` green (193 passed, 10 skipped). The integration
suite needs PostgreSQL + Redis and was not run here — it is exercised by CI's `test-integration` job.

### 0A. Deploying without Docker — PaaS, API + workers (2026-11-27)

The deployment target is a platform (Render / Railway / Fly / Heroku), not a compose stack, and it
must run the API **and** the background workers. Docker remains only the build recipe the platform
executes. New in this pass:

1. `backend/scripts/start_api.sh` — honours `$PORT`, `WEB_CONCURRENCY` (default 1), `UVICORN_KEEP_ALIVE`
   (default 75 s, longer than a proxy's ~60 s idle timeout), `FORWARDED_ALLOW_IPS` and
   `MIGRATE_ON_START`; runs `python -m uvicorn` so it works without console scripts on `PATH`.
2. `start_worker.sh` / `start_beat.sh` — same interpreter handling, queues `media,transcribe,analyze`,
   `--max-tasks-per-child`, a configurable beat schedule file, and a warning that beat is
   single-instance by definition.
3. `release.sh` + `app/scripts/migrate.py` — release-phase migrations under a PostgreSQL advisory
   lock, non-zero exit on failure, `--check` mode, and a URL resolved from the environment so the hook
   needs **only** `DATABASE_URL` (a missing Redis or S3 setting fails the web deploy instead, with a
   message naming it).
4. `app/core/preflight.py` — startup self-check. Fatal: unusable `TMP_ROOT`/storage root, local
   storage in production, S3 not configured. Warnings (startup continues): missing FFmpeg, empty ASR
   weight cache, public docs in production, wildcard `TRUSTED_HOSTS`, insecure session cookie, rate
   limiting disabled, `TMP_ROOT` smaller than `MAX_UPLOAD_MB`. Each is logged as `app.preflight`.
5. `backend/Dockerfile` — `production` is now the **last** stage; with `dev` last, a plain cloud build
   silently deployed a `--reload` server. Non-root uid 10001, `tini`, HEALTHCHECK on
   `/api/v1/health/readyz`.
6. `docs/DEPLOY.md` + `deploy/fly.toml.example` — component table, full env list, per-platform release
   hook and start commands, troubleshooting table keyed on the exact log lines above.

Verified here with real processes (no Docker, no database): the API started through
`./scripts/start_api.sh` and answered `livez` 200 with `/docs` 200 in development; the same binary
under `ENVIRONMENT=production` refused to start with
`storage_backend=local is not permitted in production` and, with S3 configured instead, started
while logging `media_tooling_missing`, `asr_model_cache_missing`, `docs_enabled_in_production` and
`trusted_hosts_wildcard`; `release.sh --check` exited 1 with the connection error and
`MIGRATE_ON_START=true` aborted before serving. `backend/tests/unit/test_deploy.py` (36 tests) locks
all of that in.

Not verified, stated plainly: no real platform build, no PostgreSQL in this sandbox, so migrations
have never been applied against a live database here, and no FFmpeg, so media paths were not
exercised. The Dockerfile was reviewed line by line, not built.

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

---

## 4C. Phase 3 (partial) — Background worker and job control (COMPLETE)

Phase 2 ended with a stated limitation: `POST /analyze` ran the whole pipeline inside the request,
so a long video held a request thread for its duration. That is now fixed, and the endpoints that
were missing from the designed contract exist.

### What was built

- `app/worker/` — Celery app with three queues split by cost profile (`media`, `transcribe`,
  `analyze`), so a slow transcription cannot starve a cheap scoring job. `acks_late` and
  `prefetch_multiplier=1` so a killed worker's message is redelivered rather than lost.
- `app/worker/tasks.py` — `analysis.run` calls the **same** `AnalysisService` the synchronous path
  used. There is one implementation of the pipeline, not two that drift. Tasks take ids, never
  objects, and re-read state, so a retry sees current data.
- `app/worker/dispatch.py` — the honest-fallback decision described below.
- `analysis.sweep_stuck` — reclaims `RUNNING` jobs whose heartbeat went quiet because their worker
  was killed. It moves them to `DEAD_LETTER` and does **not** silently retry.
- `app/api/v1/jobs.py` — `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`,
  `POST /jobs/{id}/retry`. The previous list/get pair in `system.py` was removed rather than
  duplicated (it was returning a bare array, which also violated the paginated-schema contract).
- `AnalysisService.plan()` / `analyse()` split — validation and job reservation happen *before*
  dispatch, so an illegal request fails with a precise 4xx instead of enqueueing work doomed to
  fail minutes later.

### Live evidence (real Redis, real Celery worker process, real PostgreSQL, real Whisper)

```
POST /analyze  ->  202 in 0.113 s          (was 3.69 s blocking, and O(video length) before)
poll GET /jobs/{id}:  PENDING -> PENDING -> PENDING -> SUCCEEDED 100 "5 candidates"
project:  status=MOMENT_DETECTION segments=5 words=65 candidates=5 selected=1
journal:  ... SEMANTIC_ANALYSIS -> MOMENT_DETECTION [select_candidates]

repeat /analyze (no force) -> queued=false, no new job row
/analyze?force=true        -> queued=true,  new job, re-transcribes

cancel a SUCCEEDED job -> 409 job_not_cancellable
retry  a SUCCEEDED job -> 409 job_not_retryable  (re-running good work is ?force=true)
no CSRF                -> 403
other tenant's job     -> 404   (list and detail)
anonymous              -> 401

simulated killed worker (job RUNNING, heartbeat 2h old):
  analysis.sweep_stuck -> {'reclaimed': [...], 'count': 1}
  job became DEAD_LETTER / worker_lost
```

### The honest-fallback rule

When the broker is unreachable there are exactly two acceptable behaviours, and which one applies
depends on configuration:

- `CELERY_TASK_ALWAYS_EAGER=true` — run in-process via `.apply()`. Same task body, same database
  writes. Used by tests and local development.
- otherwise — raise `BrokerUnavailableError`, and the API returns **503 `queue_unavailable`**. The
  job row is already committed as `PENDING`, so the work is discoverable and retryable once the
  broker returns.

The third option — silently running the pipeline inside the web request — was rejected. It would
make a misconfigured production deploy look healthy while blocking a request thread for minutes,
which is the exact failure this change exists to remove.

### Review, rejection and version restore

A rendered clip is frozen. It cannot be rendered over: a file a person has already judged must not
change underneath them. Rejecting is what grants permission to render again, and it requires a
reason — a refusal nobody can act on is not recorded as one.

```
render                  -> 201, v1, QA_PASSED, publishable true
render again            -> 409 clip_not_renderable        (frozen)
reject {}               -> 422                             (no reason given)
reject {"reason": "  "} -> 422                             (blank reason)
reject {reason}         -> 200, REJECTED, publishable false, reason stored, metadata.rejected_at
render                  -> 201, v2                        (the rejection unlocked it)
versions                -> [2: 2573438 B, 1: 2573438 B]    (both kept, both with sha256)
restore v1              -> 200, current_version_id = v1, state QA_PASSED
restore v1 again        -> 409 already_current
versions after restore  -> 2                              (restore deletes nothing)
```

Restore is a pointer move, not a re-encode: every version is immutable and its file is still in
storage, which is the whole reason the version table is append-only. A version id is reachable only
through its own clip and workspace, so passing another clip's version id is a 404, not a 403.

### Bugs found while building this (all fixed)

1. **A job left dangling in `PENDING` forever.** When a retried job found an existing transcript it
   returned early without closing the job. An adopted job must always reach a terminal state.
2. **Retry did not redo the work.** `retry` dispatched without `force`, so the task reused the prior
   transcript and returned in 0.07 s. A retry that changes nothing is a lie; it now forces a re-run.
3. **The retry gate ran before the resume.** The `_REPEATABLE_STATUSES` check rejected a `FAILED`
   project before `_resume()` could move it, because the resume block was placed after the gate.
4. **`FAILED`/`CANCELLED` had no outgoing edge in the stage graph**, so a retry could re-run the
   pipeline while the journal silently skipped every stage — leaving a project marked `FAILED` with
   a fresh transcript underneath it. Added the single `-> MEDIA_INSPECTION` edge, reachable only by
   an explicit retry, and documented why that does not contradict the "absorbing" invariant.
5. **`AnalysisError` was not picklable**, so the worker log was full of
   `UnpickleableExceptionWrapper(...)` instead of the real reason. It now reduces to JSON-safe
   primitives.
6. **`DEAD_LETTER` was unreachable.** `_fail_job` hardcoded `retryable=False`, so a transient
   failure was always labelled permanent. Retryability is now threaded from the failure type.
7. **`celery.exceptions.OperationalError` is a kombu error, not a `CeleryError`** — so a broker
   outage escaped the handler and became a 500 instead of the intended 503. Caught by a test.
8. **Eager mode swallowed task exceptions** (`task_eager_propagates` defaults to False), which
   would have turned a `422 no_speech_detected` into a `202`. Eager mode now propagates.
9. A test asserted on `Row.t`, which is a deprecated SQLAlchemy builtin attribute — it silently
   returned the wrong value. Renamed the aliases.
10. Duplicate operation IDs: a jobs list/get pair already existed in `system.py`.

Also fixed a gate failure this surfaced: the new `job_stuck_after_seconds` setting was not
documented in `.env.example`, which `phase0_verify.sh` checks.

### Still true, and still not claimed

- **Resumability after a killed worker is still not claimed as a feature.** The sweeper *detects*
  and *surfaces* a lost worker (`DEAD_LETTER/worker_lost`) and a human or the retry endpoint can
  restart it. The pipeline does not itself resume from a mid-stage checkpoint; a retry redoes the
  analysis from the top. This distinction is deliberate — "resume" and "retry" are not synonyms.
- **No Celery beat process for the sweeper yet.** `analysis.sweep_stuck` exists and works when
  invoked, but nothing schedules it. Until beat is deployed, a lost worker needs a manual sweep.
- The synchronisation section of the plan below is otherwise unchanged.

## 4D. Phase 3 (partial) — Clip rendering with captions (COMPLETE)

The Phase 2 pipeline ends with ranked candidate moments. This slice turns a moment into a file a
person could actually post: a vertical 1080x1920 clip, cut on the moment's own window, with
captions burned from the word timings the ASR recorded.

### What was built

- `app/services/captions.py` — cue construction from stored word timings only. `words_from_segments`
  flattens the persisted `transcript_segments.words` payload; words with no timing are **dropped and
  counted**, never given an invented one. Cues break on a maximum word budget and on a silence gap,
  are re-based to clip-relative time (`0` = clip start), and are truncated at the clip end with the
  truncation reported. Emits WebVTT and ASS.
- `app/services/render.py` — `Template.load` reads `config/templates/*.yaml` (resolved against the
  repo root, see the bug below); `nudge_to_word_gap` moves a cut off a word boundary and *records*
  the delta; `build_plan` produces a serialisable `RenderPlan`; `render_clip` runs FFmpeg and then
  **verifies the output** with `ffprobe` — exact canvas size and a real audio stream, or it fails.
- `app/services/clips.py` — `ClipService.generate()` (idempotent, one clip per selected moment) and
  `ClipService.render()` (one new `clip_versions` row per render, storing the clip file, the ASS
  sidecar, the plan and a sha256 of the output).
- `app/api/v1/clips.py` — 8 routes: `POST .../generate-clips`, `GET .../clips`,
  `GET .../clips/{id}`, `POST .../clips/{id}/render`, `GET .../clips/{id}/versions`,
  `GET .../clips/{id}/render-plan`, `GET .../clips/{id}/captions`, `GET .../clips/{id}/download`.
- `app/api/v1/clips.py` also carries **reject** and **version restore**: a rendered clip is frozen
  until a human rejects it with a reason, and restoring an earlier version moves a pointer rather
  than re-encoding, because versions are immutable.
- `app/db/repositories.py` — `ClipRepository`; `app/api/schemas.py` — the clip response models.

The implemented surface is now **36 operations / 32 paths / 36 schemas** (reject and version
restore were added after the first pass; see below). The reviewed contract in
`docs/openapi.json` is unchanged at 62 operations / 53 paths / 43 schemas.

### Live evidence (real FFmpeg, real libass, real Whisper output, real PostgreSQL)

```
POST /generate-clips -> 201 {created: 1, existing: 0, candidates_selected: 1}
POST /generate-clips again -> 201 {created: 0, existing: 1}      (idempotent)

GET /clips
  "When I started, I spent three weeks copying e…"  window 7880-19680 ms  state DRAFT
  metadata.category / rationale / generated_from all present

GET /clips/{id}/captions
  10 cues, coverage 1.0, words_without_timing 0, warnings []
  cue 0:      0 -  1240  "When I started, I"
  cue 9:  10520 - 11800  "about and finish it."
  every cue <= 4 words (the template's budget), VTT emitted

POST /clips/{id}/render -> 201
  QA_PASSED, 1080x1920, 11800 ms, 2573439 bytes, 6.29 s wall
  has_captions true, cue_count 10, publishable true
GET /clips/{id}/versions -> 1 version, sha256 recorded, 2513 KB
GET /clips/{id}/render-plan -> window 7880-19680, source 640x360,
  adjustments {start_delta_ms: 0, end_delta_ms: 0, reason: []}

POST /clips/{id}/render on a QA_PASSED clip -> 409 clip_not_renderable
```

The rendered file was then re-probed independently (`ffprobe` on the stored bytes):
`h264 1080x1920 + aac audio`. The claim is about the bytes, not the plan.

A frame was extracted and looked at, which is how the two geometry bugs below were found.

### Review, rejection and version restore

A rendered clip is frozen. It cannot be rendered over: a file a person has already judged must not
change underneath them. Rejecting is what grants permission to render again, and it requires a
reason — a refusal nobody can act on is not recorded as one.

```
render                  -> 201, v1, QA_PASSED, publishable true
render again            -> 409 clip_not_renderable        (frozen)
reject {}               -> 422                             (no reason given)
reject {"reason": "  "} -> 422                             (blank reason)
reject {reason}         -> 200, REJECTED, publishable false, reason stored, metadata.rejected_at
render                  -> 201, v2                        (the rejection unlocked it)
versions                -> [2: 2573438 B, 1: 2573438 B]    (both kept, both with sha256)
restore v1              -> 200, current_version_id = v1, state QA_PASSED
restore v1 again        -> 409 already_current
versions after restore  -> 2                              (restore deletes nothing)
```

Restore is a pointer move, not a re-encode: every version is immutable and its file is still in
storage, which is the whole reason the version table is append-only. A version id is reachable only
through its own clip and workspace, so passing another clip's version id is a 404, not a 403.

### Bugs found while building this (all fixed)

1. **Captions covered the middle of the frame.** `per_cue` was computed as
   `max_words_per_cue * max_lines`, so a 4-word style produced 8-word cues that wrapped to four
   lines. `max_words_per_cue` already means "words visible at once"; the multiplication was simply
   wrong. Now `per_cue = max_words_per_cue`.
2. **The caption block sat too high.** `lower_third` used a 35%-of-script-height bottom margin,
   which with the script resolution put it over the subject. Now 14%; a frame was re-extracted and
   confirmed at roughly 72% down the frame.
3. **Every template failed to load in the API**, though the same call worked in a script run from
   the repo root: the template directory was resolved relative to the process CWD. Anchored to
   `__file__` via `resolve_template_dir()`.
4. **A missing template returned 500 instead of 422.** `Template.load` sat *above* the
   `try/except RenderError`, so the error escaped the handler. Loading is now inside the guard and
   translates to `ClipError(422)`.
5. **Captions were built from the pre-nudge window while the render used the post-nudge one.**
   The two files would have described slightly different intervals, so a cue could begin before
   the picture did. The cut is now resolved *first*, and both the caption track and the plan are
   derived from that one authoritative window.
6. **A 2.9 GB model cache had been left inside the repo** (`backend/models/`), invisible to
   `git status --short` only until it was noticed in the file list. Removed, and the path is now
   gitignored.
7. **`ctx.user_id` does not exist** — the workspace context object exposes `ctx.user`, so the
   reject route raised `AttributeError` and returned 500 on the happy path. Caught by running the
   cycle against the live API before writing its test.
8. **`ClipService(db)` was constructed without its settings and storage arguments** in the two new
   routes: the service has no defaults for them by design, so the route failed at call time rather
   than at import time. Both now go through the shared `_service(db)` factory.
9. **`clips.meta` does not exist.** The DDL column is `metadata`, but `metadata` is taken by
   SQLAlchemy's schema registry on a declarative class (and assigning it raises), so the mapped
   attribute is `meta` with `mapped_column("metadata", ...)`.
10. **`Mapped[bytes | None]` could not be resolved** on `ClipVersion.sha256`: inside a `Mapped[]`
   annotation the name `bytes` resolves against the enclosing class's own `bytes` column. Annotated
   as `Mapped[LargeBinary | None]`.
11. The new `template_dir` setting was missing from `.env.example`, caught by `phase0_verify.sh` —
   the same gate that caught `job_stuck_after_seconds` in §4C.

### What is deliberately not claimed

- **The live fixture happens to cut on clean word boundaries**, so its render plan shows
  `start_delta_ms: 0, end_delta_ms: 0`. The nudge path is exercised by an integration test that
  moves a stored window 40 ms into a word and asserts the cut snaps back and the reason is recorded.
- **Regeneration produces the same cut.** A rejected clip can be rendered again, but `regenerate`
  (a shorter window, a different hook) is not implemented — the moment's stored window is the only
  one that can be rendered. Version 2 is therefore a fresh encode of the same edit, useful for
  proving the versioning and for recovering from a render fault, not a new creative choice.
- **No subjective/visual QA on the rendered clip itself.** `QA_PASSED` here means "the render was
  verified structurally", not "the clip looks good". The Phase 2 visual metrics have not been
  reused as the QC report yet.
- **Sharpness/quality is capped by the source.** The fixture is 640x360; upscaling it to 1080x1920
  is honest arithmetic, not recovery of detail.
- **No signed download URLs with the local storage backend** — `download_url` is `null` and the
  response says so, rather than returning a link that would 404.
- **Beat is still not deployed**, so `analysis.sweep_stuck` remains unscheduled (§4C).

## 4E. Phase 3 (partial) — The web frontend (first complete slice)

Until this point the frontend was a Dockerfile and nothing else: the API was fully exercisable over
HTTP and there was no way to use the product. This is the first slice that makes the pipeline a
thing a person can sit in front of.

### What was built

Next.js 15 App Router, TypeScript strict, no UI framework — a hand-written design system in
`app/globals.css`, because a handful of screens did not justify the dependency or the bundle.

| Route | What it does |
| --- | --- |
| `/login`, `/register` | Session start, with the API's field errors surfaced per input |
| `/dashboard` | Counts, per-stage breakdown, recent projects |
| `/projects` | List + inline create |
| `/projects/[id]` | **The core screen** — upload with progress and rights statement, analyse, candidate moments with expandable component scores, per-clip render/reject/versions/captions, transcript with untimed words marked, stage journal |
| `/clips/[id]` | A shareable address for one clip |
| `/jobs` | Every stage run, with cancel/retry, polling only while work is in flight |

`app/error.tsx` is a route-level boundary: one unguarded field on one panel must not blank the app.

### How it talks to the API

The browser only ever talks to its own origin. `next.config.mjs` rewrites `/api/v1/*` to the API,
so the session cookie is first-party and **CORS stays out of the auth story entirely**. The browser
never learns the API's internal address.

### Bugs found by building the UI (all fixed)

Building the UI was, as usual, the most effective review the API has had.

1. **`GET /projects` returned 500 for any project that had a source.** The list route validated the
   ORM straight into `ProjectOut`, and `SourceVideo.sha256` is `bytes`, which the schema refused as
   a string. Every existing test listed a *source-less* project, so the bug was invisible for three
   phases. The detail route had already solved this by hand; the fix generalises that solution into
   `ProjectOut.from_model` and uses it in all three call sites (create, list, patch) and the
   dashboard's recent-projects list. Regression tests added for both the list and the dashboard.
2. **`GET /auth/me` could not be used to resume a session.** It returns the user, and the server
   stores only a *digest* of the CSRF token, so a reloaded page had no way to obtain a token and
   every subsequent mutation 403'd forever. The app was therefore unusable after any reload.
   Fixed by publishing the token as a second, readable, non-authoritative cookie (`ac_csrf`) and
   refreshing it on `/auth/me`. Double-submit still holds: the cookie is never trusted alone, only
   compared against the header and the stored digest.
3. **`generate-clips` returned 500 after writing its rows.** The structured log call used
   `extra={"created": ...}`; `created` is a reserved `LogRecord` attribute, so `makeRecord` raised
   `KeyError` — a logging mistake turned into a failed request for work that had already succeeded.
   Renamed, and a test now scans every `extra=` in `app/` against the reserved set.
4. **The project page crashed the whole app on a guessed response shape.** The UI's `RenderPlan`
   type had invented a `layout` object and an `audio` block; the real plan has neither. The
   unguarded read replaced the entire screen with Next's generic error page. Fixed by copying the
   type from a live response, and by adding the route error boundary so this class of mistake
   degrades to one panel instead of the whole app.

### Verified in a browser, against the real stack

Driven with Playwright against the running API, not asserted from a mock:

```
login -> dashboard          real counts (1 project, 1 source, 0 failed jobs, 0.9 MB)
projects -> project         upload 640x360 19.8 s fixture, analyse, 5 moments, 65 words
clip card                   "When I started, I spent three weeks copying examples…"
                            rendered: 1080x1920, 2.5 MB, 10 cues, 100% coverage
caption panel               10 cues, 4 words each, 00:00.0-00:01.2 first
reject via the UI           typed reason -> REJECTED, reason shown, render unlocked
render again via the UI     v2 produced, both versions listed, current = v2
score disclosure            14 component bars + the stored rationale text
console                     no page errors (only the expected pre-login 401)
```

Navigation, session persistence across reloads, and every mutation were exercised by clicking.

### Not done in this slice

- **No video playback.** The local storage backend exposes no signed URL, so a rendered clip is
  *described* (dimensions, size, version) rather than played. That is honest but it is not a
  preview; a storage backend with signed URLs, or a streaming proxy route, is the missing piece.
- **`regenerate`, `approve`, `publish` and `schedule`** have designed routes and no implementation;
  the UI does not offer them.
- **No project settings screen.** Template, durations and target platform can only be set at
  creation.
- **No upload resume**, and no client-side validation of codec or duration before the bytes go up —
  a bad file is rejected by the server after the transfer.
- **The frontend has no automated tests.** It was verified by driving it, which catches far more
  than nothing but does not stop a regression. Playwright is installed in the sandbox; a smoke test
  per route is the obvious next step.

## 5. Outstanding limitations (stated, not hidden)

### Expected at this phase
1. ~~**No frontend yet.**~~ **Done — see §4E.** The dashboard, project workspace, clip review and
   jobs screens all exist and were verified in a browser against the live API. Remaining frontend
   gaps (no in-app playback, no automated UI tests) are listed at the end of §4E.
2. **Publishing is not implemented.** Analysis, clip generation and 9:16 rendering now work (§4C,
   §4D); YouTube upload remains specified-only.
3. **No frontend yet** — see item 1.
4. **Timeline thumbnails and waveform peaks** do not exist yet. A rendered 9:16 clip does exist and
   is retrievable, but there is no preview player in a UI.

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
1. ~~Celery worker + beat, convert `/analyze` to enqueue, `GET /jobs*` and
   `POST /jobs/{id}/cancel|retry`.~~ **Done — see §4C.** Remaining from this item: a beat schedule
   for `analysis.sweep_stuck`, and genuine mid-stage checkpointing if true resumability is wanted
   (today a retry restarts the analysis, it does not resume it).
3. ~~**Clip rendering.** Cut, reframe to 9:16, burn captions from the stored word timings, persist
   `clips` + `clip_versions`.~~ **Done — see §4D**, including review, rejection and version
   restore. Remaining: `regenerate` with a different window/variant, `approve`, and reusing the
   Phase 2 visual metrics as a real quality report.
4. **Quality check** — reuse the Phase 2 visual metrics as the objective half of the QC report.
5. **YouTube OAuth and publish** — needs real credentials; until then the honest
   `youtube_not_configured` path stays.
6. ~~**Frontend** — dashboard, create-project, upload with progress, transcript viewer, moment list
   with per-component scores visible.~~ **Done — see §4E.**
7. **A production ASR model and a real accuracy measurement.** `tiny.en` proved the pipeline, not
   the product; word error rate on actual source material is unmeasured.

**Exit criteria:** a user can upload a video, watch progress update asynchronously, review a
ranked moment list with explainable scores, and receive a rendered 9:16 clip with correctly timed
captions — with every number traceable to real measured data and no fabricated output anywhere.

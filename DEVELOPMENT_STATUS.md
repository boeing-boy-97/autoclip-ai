# DEVELOPMENT STATUS

**Product:** AutoClipper AI — autonomous video intelligence, clipping & YouTube publishing
**Current phase:** Phase 0 — Architecture and project foundation — **COMPLETE**
**Date:** 2026-11-26
**Verification command:** `./ops/scripts/phase0_verify.sh` → **20 checks passed, 0 failed**

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

## 5. Outstanding limitations (stated, not hidden)

### Expected at this phase
1. **No domain code yet.** Auth, projects, uploads, analysis, clips, publishing are Phase 1+.
   `docs/openapi.json` specifies them; the app currently serves only health + error contract.
2. **Frontend is scaffolding only** (directory structure + Dockerfile). No pages.
3. **Alembic migrations not yet authored.** `schema.sql` is authoritative; Phase 1 generates
   revisions from it and adds a drift check.

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

## 6. Next phase — Phase 1: Authentication and project management

**Build:**
1. `core/security.py` (Argon2id, session tokens, CSRF), `db/session.py` (async SQLAlchemy + pool).
2. SQLAlchemy models for users, workspaces, members, sessions, projects, source_videos, media_assets,
   project_stage_events + first Alembic revision matching `schema.sql` exactly.
3. `POST /auth/register|login|logout`, `GET /auth/me` with real session cookies.
4. Workspace authorization dependency; repository layer with mandatory `workspace_id`.
5. Projects CRUD + the declarative `STAGE_GRAPH` state machine (single source of truth, unit-tested).
6. Upload path: streaming size cap, magic-byte + MIME + extension + `ffprobe` validation, SHA-256,
   storage abstraction (local + S3), signed URLs.
7. Dashboard summary endpoint backed by real counts, with useful empty states.
8. Frontend: app shell, auth pages, dashboard, create-project flow, project list/detail.
9. Integration tests: migrations up/down, workspace isolation matrix, upload rejection cases.

**Exit criteria (must be demonstrated, not asserted):**
registration → login → create project → upload a fixture → real `ffprobe` inspection recorded →
source state `ready` → project in `INGESTING`/`MEDIA_INSPECTION` with a correct `project_stage_events`
journal; corrupt/wrong-MIME uploads rejected with specific codes; a second workspace cannot read any
of it (404); dashboard numbers equal the database.

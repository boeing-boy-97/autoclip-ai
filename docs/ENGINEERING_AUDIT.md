# Engineering audit and repair report

Date: 2026-11-27 · Current branch: `arena/e3e7743a-autoclip-ai`

Earlier merged pass: `arena/fdfac07f-autoclip-ai` (PR #2).

## Executive assessment

This is an implemented hardening/repair pass, **not certification that the entire application is production-ready**. Concrete defects were repaired and regression tests added. The follow-up now verifies real PostgreSQL/Redis API workflows and real FFmpeg rendering. Complete speech analysis, deployment images, external providers, and browser end-to-end behavior remain unverified. Only disposable local test databases were migrated and modified; no production data was touched. No secrets were intentionally printed or committed.

## Follow-up verification and repairs (current branch)

**This section supersedes the earlier pass's verification counts and claims of unavailable local database/media infrastructure below.** It does not mark the audit complete or waive the remaining release gates.

### Implemented follow-up fixes

| Priority | Defect / repair | Principal files |
|---|---|---|
| High | Cancelled jobs could be overwritten by worker progress/success/failure; duplicate deliveries could run concurrently. Row-locked job transitions, project execution advisory locks, durable checkpoint commits, retired-delivery checks and pre-publish delivery identity now protect these paths. Retry waits for the previous execution to stop. | `db/locks.py`, `db/repositories.py`, `services/analysis.py`, `worker/{dispatch,tasks}.py`, `api/v1/jobs.py` |
| High | Project mutation/deletion, clip generation, restore/reject and rendering could race running analysis/render. Shared nonblocking project guards now return 409; render commits its version before releasing execution ownership. Forced analysis cannot replace transcript/candidate history after clips exist. | `api/v1/{projects,analysis,clips}.py`, `services/{analysis,clips}.py` |
| High | Stuck-job sweep could retire a slow but live worker. It now checks execution ownership and rechecks locked state. Never-dispatched PENDING and abandoned RETRYING jobs become visible DEAD_LETTER failures after the configured cutoff rather than blocking the project forever. This is recovery visibility, **not** transactional delivery. | `worker/tasks.py` |
| High | Configured session cookie names were ignored by authentication/logout. Requests now read the configured name. | `api/{deps,v1/auth}.py` |
| High | Old clip-version links resolved the current version. Media now accepts tenant/clip-scoped `version_id`; frontend links pin their displayed version. Source materialization uses LocalStorage containment checks and does not accidentally read local files for S3 storage. | `api/v1/clips.py`, `db/repositories.py`, `services/{clips,analysis}.py`, `frontend/components/ClipCard.tsx` |
| High | Template traversal and decoder protocol risks; template keys are validated at schema and loader boundaries and media subprocess inputs allow local file protocol only. | `api/schemas.py`, `services/{render,transcription,visual}.py` |
| Medium | Redis increment/expiry split could create persistent rate-limit keys; both writes now execute atomically in transactions. S3 permission/outage errors no longer masquerade as missing files. | `core/ratelimit.py`, `services/{auth,storage}.py` |
| Medium | Optional vector-extension failure rolled back mandatory baseline work; savepoint isolates it. Migration command honors unpooled URL alias. CI integration service absence now fails rather than silently skipping. | baseline migration, `scripts/migrate.py`, integration conftest, `.github/workflows/ci.yml` |
| Medium | Aspect-ratio-dependent visual byte interpretation was incorrect. Frame output is explicitly bounded/letterboxed, sample count capped, and incomplete raw frames rejected. | `services/visual.py` |
| Medium | Blank project names, arbitrary publishing modes and unsafe template keys reached persistence; schema validation now rejects them. Unimplemented publishing/diarization are no longer advertised as available. | `api/schemas.py`, `core/config.py` |
| Medium | Timestamp-only pagination lost rows with identical timestamps. Composite timestamp/UUID cursors and deterministic ordering fix project/job/clip pagination while accepting old cursors. Projects UI adds a load-more control. | `db/repositories.py`, `frontend/app/projects/page.tsx` |
| Medium | Blocked localStorage, stale concurrent session refreshes, and temporary API outages could misrepresent authentication. Safe preference helpers, refresh revision protection, retryable error UI and workspace/route state resets added. Recursive structured log redaction now follows exception formatting; broker logs omit exception text. | `frontend/lib/preference.ts`, `frontend/lib/session.tsx`, `frontend/components/Shell.tsx`, `core/logging.py`, `worker/dispatch.py` |

### Latest executed verification

| Check | Result | Evidence / scope |
|---|---|---|
| Dependency installation | PARTIAL PASS | Working local backend test/runtime subset and npm clean install. Heavy ASR/model/provider environment is not installed in full; do not treat this as a clean full requirements install. |
| Python Ruff lint / formatting | PASS | 63 backend source/test files formatted; checks clean. |
| Python mypy | PASS | 43 application modules. |
| Backend unit suite | PASS | 236 passed. |
| Combined backend unit + integration | **FAIL / PARTIALLY BLOCKED** | **308 passed, 2 failed, 61 skipped**. Both failures expect silent-input `422/no_speech_detected` but get truthful `503/asr_engine_missing`: local faster-whisper/model unavailable. Skipped speech/provider cases are not passes. |
| New execution/media integration suite | PASS | **11 passed**, real disposable PostgreSQL 16.2, Redis 6.2.14, FFmpeg/FFprobe; includes threaded cancellation/duplicate execution, busy mutation controls, sweeper ownership, equal-timestamp pagination, custom cookie, real render/range/tenant/version tests. Controlled pipeline doubles test lifecycle races, not ASR accuracy. |
| Auth/projects/sources API subset | PASS WITH SKIPS | 58 passed, 2 skipped in targeted execution before later execution-suite additions. These paths were also included in the combined final run. |
| Frontend ESLint / TypeScript | PASS | Latest changes included. |
| Frontend unit/component tests | PASS | 8 passed across 3 files; includes blocked preferences/outage/stale refresh. |
| Frontend production build | PASS | Next 15.5.29; nine routes generated. |
| OpenAPI | PASS | Implemented JSON regenerated (37 operations); design-contract validator reports 62 operations / 53 paths. These are different schemas, not a claim all design operations exist. |
| npm audit | PASS | 0 known vulnerabilities, including dev dependencies in final audit. |
| Bandit | PASS at CI severity threshold | No medium/high findings (`-ll`); full scan reports 12 low findings, not a clean penetration test. |
| pip-audit (installed environment) | PASS after local tool maintenance | Initial audit found 21 advisories/duplicate entries in old pip/setuptools; upgrading those **local venv tools** yielded no known vulnerabilities. Heavy omitted dependencies and built container are not covered. |
| Git whitespace | PASS | `git diff --check`. |
| Browser E2E, Docker image/full Compose, live S3/providers | BLOCKED / NOT EXECUTED | No browser/Docker execution or external credentials/model access established here. |

Local aggregate test log: `/tmp/audit-final-tests.log`; dedicated execution tests: `/tmp/audit-execution.log`; frontend build: `/tmp/audit-frontend-build.log`. These sandbox logs are ephemeral, not deliverables or committed artifacts.

### Remaining work / release gates — do not certify production

1. **Durable dispatch:** database job reservation and broker publish are still not one atomic outbox transaction. Delivery identity and stale-PENDING recovery reduce damage but do not guarantee automatic exactly-once delivery. A crash can require explicit operator retry. Run real broker/worker crash/redelivery and load tests, including retry-limit races.
2. **PostgreSQL connection mode:** execution locks require **direct or session-pooled `DATABASE_URL`**, not transaction pooling. The migration-only unpooled URL does not change runtime lock connections. Each active execution reserves an additional connection; size the pool for API and worker concurrency. Test failover/connection loss before deployment.
3. **Speech pipeline:** install supported faster-whisper and locally available model, generate the real speech fixture, rerun the two failing cases and all skipped speech/analysis/render pipeline cases. No ASR text/QA was fabricated to obtain green tests.
4. **Missing designed workflows:** independent QA/approval/repair, publishing and analytics are not complete. They require real implementation and product acceptance, not boolean flags. Publishing and diarization availability remain false. Do not auto-approve based on successful rendering.
5. **Operational integrity:** API rendering remains synchronous; orphan object cleanup, upload allocation/restore races, external storage compensation, resource quotas and provider timeout/recovery/load tests remain. Cancellation is cooperative at durable checkpoints, not interruption of FFmpeg/ASR.
6. **Frontend:** jobs and project-detail lists still need complete pagination coverage, broader action/error/cancellation tests and actual responsive/accessibility/browser E2E verification. Projects load-more is implemented but has no dedicated browser regression yet.
7. **Security/deployment:** live S3 presigner TTL/CORS, proxy trust, Redis fail-open policy, full clean locked Python environment/container dependency audit, container startup/health/migrations and secrets/TLS production configuration remain release gates. Dependencies still use broad lower bounds; installed-environment audit is not a reproducible production SBOM.
8. **Audit coverage:** not every historical document and every service/provider/error branch has been individually verified. The remaining review is not being represented as finished merely because lint/build pass.

### Reproducing the local integration checks

Use dedicated disposable-test PostgreSQL admin credentials and Redis database; never production infrastructure. This sandbox cluster is SQL_ASCII, so `PGCLIENTENCODING=UTF8` is required here (a normal UTF8 cluster should be preferred). Required PostgreSQL extensions are installed; no production database was migrated.

```bash
# From repository root, after installing dev requirements and generating fixtures:
(cd backend && PGCLIENTENCODING=UTF8 PATH="$PWD/../.cache/audit-bin:$PATH" \
  ../.venv/bin/pytest tests/unit tests/integration -o addopts='-q')
# For ordinary hosts put real ffmpeg/ffprobe on PATH rather than the sandbox cache.
(cd backend && ../.venv/bin/mypy app --ignore-missing-imports)
.venv/bin/ruff check backend/app backend/tests
.venv/bin/ruff format --check backend/app backend/tests
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend audit
.venv/bin/bandit -r backend/app -ll
.venv/bin/pip-audit --vulnerability-service pypi
```

## 1. Repository overview

Original stack: Python/FastAPI/Pydantic, synchronous SQLAlchemy with PostgreSQL-specific models and Alembic baseline SQL, Redis/Celery, FFmpeg/FFprobe, local/S3 storage, local Whisper or external ASR/AI integrations, Next.js 15.5/React 19/TypeScript, CSS design tokens, Docker Compose, GitHub Actions. Root Neon tooling is separate from the application's Python database access and was retained.

Execution flow: browser → same-origin Next.js `/api/v1` rewrite → FastAPI dependencies (session, workspace membership/role, CSRF) → domain services → repositories/PostgreSQL. Uploads are spooled, validated and stored; analysis is dispatched through Celery; selected moments become clip rows; synchronous rendering creates immutable clip versions and storage assets. Jobs expose durable progress. Media delivery now authorizes access before returning local file ranges or redirecting to a signed S3 URL.

Modules:
- `api/deps.py`, `services/auth.py`, `core/security.py`: opaque sessions, Argon2, roles, CSRF, credential encryption.
- `api/v1`: auth, system health, projects/dashboard, sources, analysis, jobs, clips.
- `db`: tenant-scoped repositories, ORM entities, session lifecycle, baseline migration.
- `services`: ingestion/probing, transcription, visual/scoring/analysis, captions, rendering/storage.
- `worker`: Celery configuration, dispatch, analysis task, stuck-job sweep.
- `frontend/app`: login/register, dashboard, projects/detail, jobs, clip detail.
- `config`: scoring/analysis/pricing and eight render templates.
- `ops`, `deploy`, Dockerfiles, CI: validation, migrations, startup and deployment tooling.

Inventory, configuration, key runtime/security paths, UI interactions and existing test coverage were examined. This pass did not individually verify every historical document, every service branch, or all designed API operations. The product-design OpenAPI has 62 operations; the implemented schema has 37 operations across 33 paths. A designed operation is not evidence of implementation.

## 2. Problems discovered and fixes implemented

### Security and correctness

1. **Path containment bypass**: string-prefix comparison allowed `../media-secret` beside root `media`. Local storage now uses resolved path ancestry and refuses the root itself. Tests cover sibling prefixes, absolute escapes and symlink escapes.
2. **Missing CSRF on clip mutations**: rejection and version restoration now explicitly require the existing CSRF dependency. Existing workspace authorization is retained.
3. **Unsafe publishability**: successful encoding and restoration previously set `publishable=true` without independent QA/approval. Both now produce `AWAITING_REVIEW`, `publishable=false`. Existing files remain previewable/downloadable; rejection/rerender still works. The integration expectation was updated, but that integration test could not run here.
4. **Sensitive response caching**: API responses default to `Cache-Control: private, no-store`.
5. **Untrusted trace IDs**: incoming identifiers are restricted to 1–128 safe characters; invalid values are replaced. Unhandled-error route logging now redacts UUIDs consistently.
6. **Lost HTTP headers**: framework exception translation now retains protocol headers such as `Allow`.
7. **Broker outage jobs stranded**: initial analysis dispatch and retry dispatch record recoverable failed jobs and return 503 instead of leaving undelivered PENDING work or leaking a generic 500.
8. **Incorrect filtered pagination**: publishable filtering moved into the clip repository SQL query before LIMIT/cursor construction.

### Completed existing workflow

9. **Unusable rendered output**: the download metadata endpoint previously returned no usable URL even with S3. It now provides an authenticated media URL. New media route checks clip workspace ownership and asset workspace, supports local file byte ranges via FileResponse, and uses the real storage presigner for S3. Private files are not exposed by a public static mount. S3 delivery itself still needs live-provider verification.
10. **Placeholder preview**: ClipCard now renders an actual HTML video and MP4 download link rather than describing a file that cannot be accessed.
11. **Unhandled clip-load rejection**: initial detail loading now displays a truthful error.
12. **False-success logout**: server failures no longer silently clear the UI while leaving a live session. Sign out prevents duplicate clicks and displays a retryable error.
13. **Stale CSRF token**: browser requests prefer the current readable cookie over stale in-memory state, supporting another tab's login/token rotation.
14. **Validation contract mismatch**: frontend API errors now consume the backend's `fields[{path,message}]` format while retaining legacy `errors` support.
15. **Wrong project's job**: project detail now filters newest-job requests by project ID.
16. **One-shot progress refresh**: dispatching work restarts the same continuous, bounded project polling loop rather than issuing just one delayed refresh.
17. **Workspace state reuse**: workspace-specific page trees remount when switching workspaces, preventing reuse of the old workspace's component state.

### UI, configuration and maintenance

18. Preserved the dark design system; improved secondary text contrast, narrow-screen navigation wrapping, flexible grid containment, keyboard focus visibility and reduced-motion support. Added main-navigation labeling/current-page semantics. These CSS changes were not browser/screenshot verified.
19. Docker frontend now sets its API rewrite target at **build time**, defaulting to `http://api:8000`. Runtime-only environment values cannot change compiled Next rewrites.
20. Explicit frontend tracing root removes monorepo lockfile inference and keeps standalone layout predictable.
21. Replaced broken `next lint` script with ESLint configuration and zero-warning enforcement. Added Vitest/Testing Library tests and wired frontend lint/typecheck/test/build into CI.
22. Added a compatible patched PostCSS override rather than a framework-major upgrade. Final `npm audit` reports zero vulnerabilities, including development dependencies. Dependency installation required npm 11 once to work around an npm 10 resolver crash; the generated lockfile was subsequently verified with ordinary `npm ci`.
23. Explicit pytest async fixture loop scope removes an upcoming-default warning; removed obsolete Ruff ignores rather than suppressing findings.
24. Regenerated the implemented OpenAPI schema. README now distinguishes designed QA/publishing/analytics/subject tracking from actual capabilities and explains frontend build-time configuration.

### Changed files

- Backend: `app/api/v1/{analysis,clips,jobs}.py`, `app/db/repositories.py`, `app/main.py`, `app/services/{clips,storage}.py`, `pytest.ini`.
- Backend tests: new `tests/unit/test_audit_regressions.py`; updated `tests/integration/test_clips_api.py`.
- Frontend: `components/{ClipCard,Shell}.tsx`, `lib/{api.ts,session.tsx}`, `app/projects/[projectId]/page.tsx`, `app/globals.css`, `next.config.mjs`, `Dockerfile`, `package.json`, `package-lock.json`.
- New frontend quality files: `eslint.config.mjs`, `vitest.config.ts`, `tests/{setup.ts,api.test.ts,clip.test.tsx}`.
- Supporting files: `.github/workflows/ci.yml`, `ruff.toml`, `docs/openapi-implemented.json`, `README.md`, this report.

No migrations or destructive database operations were introduced. No working storage/provider integration was replaced with a mock; test doubles exist only in tests.

## 3. Executed verification

Host: Python 3.11.2, Node 22.22.3. The documented backend target is Python 3.12. Docker, PostgreSQL client/server, Redis server and FFmpeg/FFprobe were not installed. A local Python virtual environment with web/database/security/test dependencies was installed; the full heavyweight media/provider requirements set was **not** installed.

| Check | Status | Actual result |
|---|---|---|
| Frontend installation | PASS | Final `npm ci` succeeded from regenerated lockfile |
| Full backend installation | BLOCKED | Minimal verification environment installed; full Python 3.12/media environment not exercised |
| Backend lint | PASS | `ruff check backend/app backend/tests` |
| Backend formatting | PASS | `ruff format --check backend/app backend/tests`: 61 files |
| Frontend lint | PASS | `npm run lint`: zero warnings |
| Backend type checking | PASS | `mypy app --ignore-missing-imports`: 42 source files (not strict dependency typing) |
| Frontend type checking | PASS | `npm run typecheck` |
| Frontend production build | PASS | `npm run build`: Next.js 15.5.29, all routes built |
| Backend unit tests | PASS, partial scope | 226 passed in full collection; media-dependent unit cases skipped |
| Frontend tests | PASS | 5 tests in 2 files (API/error/CSRF and rendered clip component) |
| Integration/database/auth workflows | BLOCKED | 134 total skips: 124 database-backed cases and 10 media-fixture cases; no PostgreSQL available |
| Isolated API tests | PASS | Problem responses, CSRF declarations, tenant asset check, local file HTTP 206/range body, redirects and retry failure handling |
| Real S3/provider tests | BLOCKED | No connected live storage/AI provider configuration |
| Browser E2E/responsive testing | BLOCKED | No browser E2E run; CSS inspection/component tests are not browser verification |
| Docker production image/Compose runtime | BLOCKED | Docker unavailable; frontend compilation is not an image deployment test |
| OpenAPI design validation | PASS | `python ops/scripts/validate_openapi.py`: 62 operations, 53 paths, 43 schemas |
| Implemented OpenAPI drift | PASS | Included in passing backend suite; 37 operations, 33 paths, 36 schemas |
| Security static scan | PASS, limited | `bandit -r backend/app -ll -q`: no medium/high findings; parser warnings about prose comments retained |
| Frontend dependency audit | PASS | `npm audit`: zero reported vulnerabilities |
| Backend dependency CVE audit | BLOCKED | Not executed against the full heavyweight runtime dependency resolution |
| Patch hygiene | PASS | `git diff --check` |

The backend run emitted two upstream deprecation warnings: Starlette TestClient's httpx adapter and the HTTP 422 symbolic constant. They were not suppressed. A green partial suite is not a green integration suite.

## 4. Outstanding issues and release gates

These are known remaining risks, not a complete proof that no other bugs exist:

- **Job concurrency/cancellation**: existing cancellation changes the record, not running CPU work; a worker can overwrite cancellation. Retry/redelivery and stuck-job reclamation need a transactional claim/lease model and separate-worker concurrency tests before scale-out. This pass did not alter that protocol without a working database/broker environment.
- **Dispatch atomicity**: persisting a job and publishing a broker message are still separate operations. Broker errors now become retryable failures, but crash-between-commit-and-send requires a durable outbox/reconciler for full delivery guarantees.
- **QA/approval/publishing/analytics**: independent QA reports, repair loop, human approval and official publishing/analytics workflows are not implemented. Rendering no longer pretends they passed. Existing historical `publishable=true` rows were not bulk-rewritten; no destructive/unapproved data operation was performed. Existing deployments need a reviewed reconciliation before enabling any publisher.
- **Long synchronous render requests**: FFmpeg rendering can hold API threads for minutes. Move rendering into a properly implemented durable worker flow before production load; do not merely change a queue name.
- **Database/storage consistency and concurrent renders**: upload/render object cleanup, concurrent version allocation, restoration during render and cross-process races require real PostgreSQL/S3 fault-injection tests and potentially transactional locking. Existing unique constraints are not a substitute for testing these paths.
- **Frontend data handling**: projects/jobs/moments UI generally consumes the first page; full pagination UX remains absent. Some optional data requests intentionally swallow errors into empty state and several page requests lack AbortController cancellation. Those paths need component tests and a unified error/cancellation policy.
- **Session resilience**: storage-disabled browsers, transient session-refresh outages and configurable session-cookie-name consistency need further work. Current browser CSRF cookie name remains fixed.
- **Media serving limits**: video access uses current-version semantics; browser range requests during a version switch should be tested. S3 presigned URLs are usable until their TTL expires, as designed; live CORS/content-disposition behavior is unverified.
- **Rate limiter failure policy**: existing Redis limits fail open with logs; expiry operations are separate from increments. Review outage policy/atomicity for authentication and CPU-expensive endpoints. Forwarded client-IP handling must match the trusted deployment proxy configuration.
- **Dependency reproducibility**: Python requirements remain broad version ranges without a resolved deployment lock. Validate the full set on Python 3.12 and generate reviewed constraints/locks; do not freeze this partial Python 3.11 audit environment as a production lock.
- **Infrastructure verification**: run migrations/invariants, all 134 skipped cases, real FFmpeg/caption output checks, separate Celery worker retries, actual S3 downloads, Docker startup and browser happy/failure workflows. Configure external providers only where required; no paid services were added.
- **Documentation drift**: older CODE_REVIEW/DEVELOPMENT_STATUS and architecture design sections are historical/aspirational. README now points here, but those large documents were not comprehensively rewritten.

## 5. Quality assessment

| Area | Assessment |
|---|---|
| Code quality | Improved targeted paths; Python lint/types and frontend lint/types pass |
| Architecture | Useful route/service/repository split; durable worker protocol remains incomplete |
| Frontend | Real media controls and better error/focus/mobile behavior; limited automated coverage, no browser validation |
| Backend reliability | Better error fidelity and broker recovery; cancellation/concurrency/load gates remain |
| Database integrity | Existing PostgreSQL schema/constraints retained; migrations and live integrity not verified here |
| Security | Specific CSRF/path/approval/cache issues repaired; not a complete penetration test or full CVE clearance |
| Performance | No speculative rewrites; SQL filtering corrected, polling restored; no throughput benchmark performed |
| Test coverage | 13 backend regression cases and 5 frontend cases added; no coverage percentage measured |
| Deployment readiness | **Not approved for production** until the remaining infrastructure, concurrency, QA and E2E gates are satisfied |

## 6. Run commands

Recommended runtimes: Python 3.12; Node 22.12+ (or 20.19+); PostgreSQL 17; Redis; FFmpeg with libass/libx264.

### Local full stack (Docker required)

```bash
./ops/scripts/dev_env.sh
docker compose up --build
```

### Host development

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements-dev.txt
./ops/scripts/dev_env.sh
# Configure PostgreSQL, Redis and storage values in .env before proceeding.
cd backend
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# Separate terminals, same configured environment:
celery -A app.worker.celery_app worker -Q media,transcribe,analyze -l INFO
celery -A app.worker.celery_app beat -l INFO
# From repository root:
npm ci --prefix frontend
npm --prefix frontend run dev -- --hostname 0.0.0.0
```

### Quality checks

```bash
ruff check backend/app backend/tests
ruff format --check backend/app backend/tests
(cd backend && mypy app --ignore-missing-imports)
(cd backend && pytest tests/unit tests/integration)
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend audit
python ops/scripts/validate_openapi.py
```

Generate fixtures with `./ops/scripts/make_fixtures.sh` before media tests. Integration tests create and drop a throwaway database, so provide a dedicated test PostgreSQL admin account through `TEST_ADMIN_DATABASE_URL` and dedicated Redis DB through `TEST_REDIS_URL`; never point tests at production infrastructure.

### Production frontend

```bash
API_INTERNAL_URL=http://your-internal-api:8000 npm --prefix frontend run build
npm --prefix frontend start -- --hostname 0.0.0.0
```

Use the existing backend Dockerfile/startup scripts with validated production environment values, TLS, secure cookies, private S3 and external PostgreSQL/Redis. Review `docs/DEPLOY.md` and the release gates above. Docker frontend API target is a build argument, not a runtime rewrite override.

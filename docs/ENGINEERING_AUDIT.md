# Engineering audit and repair report

Date: 2026-11-27 · Branch: `arena/fdfac07f-autoclip-ai`

## Executive assessment

This is an implemented hardening/repair pass, **not certification that the entire application is production-ready**. Concrete defects were repaired and regression tests added. Database-backed workflows, real video processing, deployment images, external providers, and browser end-to-end behavior remain unverified in this environment. No database was modified and no secrets were printed or committed.

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

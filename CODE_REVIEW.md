# Code review — autoclip-ai

> **Status (2026-11-27):** items 1–15 below were fixed on this branch, each with a regression test in
> `backend/tests/unit/test_hardening.py`; the remaining items (contract duplication, frontend tests,
> dependency lockfile, docs drift) are still open. See `DEVELOPMENT_STATUS.md` §0 for what changed.

Scope: full repository at `0a266ff` (backend FastAPI + Celery, Next.js frontend, ops, docs).
Method: read every backend module (API, services, worker, core, db), frontend app/lib/components,
CI, compose, Dockerfiles, migrations vs schema, docs vs code. Findings are ordered by value:
things that stop the system from running or that leak/allow something they should not come first,
then reliability, then tests/docs/DX.

Legend: **P0** = blocking / ships broken · **P1** = security or correctness bug · **P2** = reliability,
maintainability, DX.

---

## P0 — the stack as committed does not come up

### 1. Every Celery service in `docker-compose.yml` starts a module that does not exist
`docker-compose.yml` (worker-media … worker-analytics, beat) · `backend/app/worker/__init__.py:39,75`

All eight services run `celery -A app.workers.celery_app`, but the package is `app/worker`
(singular) and the Celery instance is `celery_app` in `backend/app/worker/__init__.py`. `celery`
fails immediately with *"Unable to load celery application"*, so `docker compose up` gives a
healthy API, seven crash-looping workers and a dead beat. Nothing in CI catches it because no job
ever starts a worker process. Fix the module path, and add a CI job that boots one worker with
`--pool=solo` and asserts `celery inspect registered` lists `analysis.run`.

### 2. The frontend image cannot be built (three separate faults)
`frontend/Dockerfile` · `frontend/next.config.mjs`

* `next.config.mjs` never sets `output: "standalone"`, but the runner stage does
  `COPY --from=builder /app/.next/standalone ./` → build fails (no such directory) or the image
  contains no server.
* `COPY --from=builder /app/public ./public` — `frontend/public/` does not exist in the repo, so
  the copy fails outright.
* The `deps` stage installs `npm ci --omit=dev`; the `builder` stage reuses those `node_modules`
  and runs `next build` on a TypeScript project whose `typescript` and `@types/*` are
  **devDependencies** → Next aborts asking for the missing TS packages.

So the compose `web` service has never built. Fix: add `output: "standalone"`, either commit a
`public/.gitkeep` or drop the copy, and install dev deps in the builder stage (`npm ci`), keeping
`--omit=dev` only for a runtime stage if needed.

### 3. Analysis cannot find its scoring config inside the container
`backend/app/core/config.py:114` · `backend/app/services/scoring.py:77-79` · `docker-compose.yml` (api/worker volumes)

`scoring_config_path` defaults to `"../config/scoring.yaml"` and is read with a bare
`Path(path).read_text()`. The image has `WORKDIR /app` and mounts `./config:/app/config`, so the
default resolves to `/config/scoring.yaml` → `FileNotFoundError` → **500 on
`POST /analysis/plan` and every `analysis.run`**. It only works when the process happens to start
from `backend/` (i.e. on a developer laptop). `template_dir` already solved this with
`REPO_ROOT` in `backend/app/services/render.py:29-42`; the scoring path needs the same treatment
(absolute repo-root resolution, or default to `config/scoring.yaml`), plus a startup check that the
file parses.

### 4. `beat` runs with no schedule, so the stuck-job sweeper never runs
`backend/app/worker/__init__.py` · `backend/app/worker/tasks.py:126` · `docker-compose.yml` (beat)

There is no `beat_schedule` anywhere in the Celery config, yet `analysis.sweep_stuck` exists and
`docker-compose.yml` starts a beat container (writing its schedule to ephemeral `/tmp`). Jobs killed
mid-flight stay `RUNNING` forever and their projects stay stuck in a stage — the exact failure the
sweeper was written for. Add a `beat_schedule` entry (e.g. every 60 s) and persist the schedule
file on a volume.

### 5. Seven queues exist, two are routed, five have no producer or consumer
`backend/app/worker/__init__.py:30-33` · `backend/app/db/repositories.py:462-475` · `docker-compose.yml`

`_TASK_ROUTES` maps `analysis.run → transcribe` and `analysis.sweep_stuck → media` only, while
`JobRepository.QUEUE_FOR_STAGE` declares twelve stages across `media, transcribe, analyze, render,
qa, publish, analytics` and compose runs one worker per queue. Consequences: `worker-analyze`,
`worker-render`, `worker-qa`, `worker-publish`, `worker-analytics` idle forever; transcription work
lands on the `transcribe` worker even though the visual/scoring work should be on `analyze`; and
nothing ever enqueues `render`/`qa`/`publish`. Either implement the fan-out (separate tasks per
stage) or collapse compose to the queues that exist — right now the topology is documentation, not
behaviour, and it doubles as a false claim of capability.

---

## P1 — authorization, CSRF, secrets

### 6. Any viewer can upload source video (`"member"` is not a role)
`backend/app/api/v1/sources.py:48,74` · `backend/app/api/deps.py:26,179`

`WorkspaceMember = _ws_dep("member")`, but `ROLE_RANK = {viewer:1, editor:2, admin:3, owner:4}`.
`ROLE_RANK.get("member", 0)` is `0`, so the check is `rank(role) < 0` → never true → a **viewer**
can `POST /sources` (upload, transcode, storage cost) although every other mutating endpoint
correctly demands `editor`. Because the string is not a validated literal, nothing flagged it. Fix
the dependency to `"editor"` and type `min_role` as a `Literal["viewer","editor","admin","owner"]`
so this cannot compile again.

### 7. CSRF is bypassable with one extra header
`backend/app/api/deps.py:105-160`

`current_auth` prefers the **cookie** over `Authorization: Bearer`, but `require_csrf` skips the
check whenever *any* bearer token is present:

> a request carrying the victim's `ac_session` cookie plus any `Authorization: Bearer garbage`
> header passes authentication (cookie wins) and skips CSRF validation.

That is a full CSRF bypass via a header an attacker page can set on a cross-origin `fetch` (subject
to preflight, but a permissive `allow_headers=["*"]` CORS config or a mis-configured proxy makes it
trivial). Decide one scheme: if the request authenticated via cookie, require `X-CSRF-Token`
regardless of what else is in the headers; if it authenticated via bearer, ignore cookies entirely.

### 8. Session/CSRF cookies are `SameSite=Lax` with `Secure` only in `production`
`backend/app/api/v1/auth.py:42-68`

`secure=settings.environment == "production"` means staging/preview deployments (HTTPS, but
`ENVIRONMENT=staging`) issue cookies over plain HTTP semantics; any `http` asset on the same site
can read the readable `ac_csrf` cookie. Base `Secure` on the request scheme / an explicit
`cookies_secure` setting, not on an environment string. Also consider `__Host-` prefix for
`ac_session`.

### 9. Rate limiting is declared but never enforced
`backend/app/core/config.py:57-58` · `backend/app/services/auth.py:187-228` · `backend/app/main.py`

`rate_limit_per_minute` and `rate_limit_upload_per_hour` exist and are documented, but the only
limiter in the codebase is `LoginThrottle` (login attempts, keyed on `(email, ip)` only — so
spraying many emails from one IP is unlimited). There is no middleware limiting uploads, `/analysis`
or `/render`. The upload endpoint is the expensive one (FFprobe + S3 + CPU); a single account can
saturate a box. Add a small Redis-backed dependency/middleware (`limits` or hand-rolled token
bucket) keyed by `user_id` for authenticated routes and `ip` for auth routes, and make
`LoginThrottle.check` degrade *closed* on Redis errors instead of raising a 500.

### 10. `LoginThrottle` has no Redis-failure path
`backend/app/services/auth.py` (`LoginThrottle.check/record_failure/clear`)

Unhandled `redis.RedisError` propagates out of `AuthService.login` → 500 on login for the whole
deployment whenever Redis blips. The rest of the codebase (`main.py` readiness) treats Redis as
degradable; auth should too — log and allow (or 503 with `Retry-After`), never 500.

### 11. Key rotation is documented but impossible
`backend/app/core/security.py` (`SecretBox` encrypt/open)

`open()` rejects any ciphertext whose `key_version` differs from the current one, so introducing a
second encryption key invalidates every stored OAuth credential and every encrypted secret at once
— while the docstring promises rotation. Implement a small keyring (`{version: key}`), decrypt by
the version embedded in the ciphertext, encrypt with the newest, and add a re-encrypt migration
path. Also add an AAD binding the ciphertext to its row/tenant so a ciphertext cannot be swapped
between rows.

### 12. `find_succeeded` is the one query with no workspace filter
`backend/app/db/repositories.py:537-544`

`backend/app/db/session.py` claims "an unfiltered query inside a workspace context is a security
bug", yet `JobRepository.find_succeeded(input_fingerprint=...)` filters only on fingerprint+status.
The fingerprint is content-based (`analysis.py:811-824`), so a job in tenant A satisfies tenant B's
idempotency gate. Today the follow-up query is scoped by `source_id`, so it does not leak rows, but
it wrongly lets B skip work (and skips a transcript B may never have produced) — and it is one
refactor away from a real cross-tenant read. Add `workspace_id`, and add the "every repository
method that takes an id takes a workspace_id" test that the comment claims exists.

---

## P2 — reliability and correctness

### 13. Health checks that cannot fail
`backend/app/main.py` (`/health`, `/readyz`, `_check_dependencies`) · `backend/Dockerfile` HEALTHCHECK · `docker-compose.yml`

* `Dockerfile` and the compose `api` healthcheck both hit `/health/livez`, which by definition
  always returns 200 — so an API with a dead database or missing FFmpeg is reported healthy and
  `web` starts against it. Healthcheck should hit `/readyz`.
* `_check_dependencies` is `async def` but calls **synchronous** `psycopg.connect()` and Redis
  `ping()`, blocking the event loop for the duration of a TCP timeout (seconds) on every probe.
  Make it `def` (threadpool) or use the async drivers.
* It also `mkdir`s the local storage root on every probe (a health check with a side effect) and
  leaks the Redis client on exception.
* `ffmpeg` is excluded from `required_ok`, so a deploy with no FFmpeg reports `ok` while every
  upload 503s.
* `/health` is unauthenticated and returns `environment` plus the full `integration_status()`
  map — a free inventory of which third-party providers are configured. Return a boolean, keep the
  detail behind auth or an internal port.

### 14. The upload handler blocks the event loop
`backend/app/api/v1/sources.py:66-145` · `backend/app/services/ingest.py:104-160`

`upload_source` is `async def`, but everything after the spooling is synchronous: `storage.put_stream`
(boto3 `upload_fileobj` for a multi-GB file), `probe_video` (subprocess up to 30 s) and the SQL
session work. While one upload is being probed/uploaded, **no other request in that worker is
served**. Either make the endpoint `def` (FastAPI runs it in the threadpool) or move the ingest to
`run_in_threadpool`. The spool write also uses `tempfile` in `/tmp` rather than `settings.tmp_root`,
so compose's `media_tmp` volume is unused and a large upload can fill the container's overlay fs.

### 15. `current_auth` and `workspace_access` silently default to a role
`backend/app/api/deps.py:105-125,178`

`role = repo.role_of(...) or "viewer"` — if the membership row is missing or the enum grows a value,
the request is *granted* viewer instead of denied. Default-deny here: `None` → 404/403.

### 16. Render/probe binaries ignore configuration
`backend/app/services/render.py:333-345` (`_probe_output`, `_has_audio`)

`render_clip` honours `ffmpeg_bin`, but the verification helpers look up `shutil.which("ffprobe")`
and silently fall back to `"ffprobe"`. On a host where FFmpeg lives at a custom path (the setting
exists exactly for that), verification fails with `probe_failed` after a successful render.
Thread `ffprobe_bin` through. Related: `_run(cmd, capture_output=True)` buffers the entire
`-progress pipe:1` stream in memory for a 15-minute render; stream/ignore it.

### 17. `main.py` is out of date with what it serves
`backend/app/main.py:1-40,90-131`

The module docstring still describes "Phase 0 — endpoints return 501", and `version` is hardcoded
rather than derived from the package. `/docs` and `/openapi.json` are exposed unconditionally (fine
for dev, an information leak in prod), and there is no `TrustedHostMiddleware`, no security-headers
middleware, and no `ProxyHeaders` configuration anywhere — so `request.client.host` for the login
throttle is the *proxy's* IP as soon as this sits behind a load balancer, collapsing the throttle to
a single global counter.

### 18. Database session/engine uses a debug-only event hook
`backend/app/db/session.py:44-52`

The `before_cursor_execute` listener either records nothing or measures time only when
`log_level == "DEBUG"`, and never logs a slow query. It is dead weight in production; either make it
a real slow-query log (`>500 ms`, with the statement, redacted) or delete it — a listener on every
cursor execution is not free.

---

## P2 — contract, tests, docs

### 19. Two OpenAPI documents, neither of which is the shipped API
`docs/openapi.json` (53 paths / 62 operations) · `docs/openapi-implemented.json` (32 paths) · `README.md`, `DEVELOPMENT_STATUS.md`

The designed contract and the implemented contract diverge by ~21 operations, CI validates the
*designed* one (`ops/scripts/validate_openapi.py`), and the README advertises 62 operations as if
they exist. Publishing, YouTube OAuth, analytics endpoints are in the contract but not in
`backend/app/api/v1/`. Pick one: generate `docs/openapi.json` from the app as the single source of
truth (`backend/app/scripts/export_openapi.py` already does this and a unit test checks freshness),
and mark unimplemented paths as `x-status: planned` in a separate design doc.

### 20. The schema and the migration are two hand-maintained copies
`docs/database/schema.sql` (709 lines) vs `backend/app/db/migrations/versions/0001_baseline.py` (899 lines)

`ops/scripts/verify_migrations.sh` exists to prove they match but **is not in CI** (the `contract`
job runs only `phase0_verify.sh`). Any future hand-edited `schema.sql` silently drifts. Make
`schema.sql` an output (`pg_dump --schema-only` in the verify script) or run the drift check in CI.

### 21. Frontend has no tests at all
`frontend/package.json` (no `test` script, no vitest/jest/playwright config) · `.github/workflows/ci.yml` (frontend job)

Not a single unit or E2E test exists for `lib/api.ts` (CSRF header, problem+json parsing),
`lib/session.tsx` (reload → `/auth/me` → workspace selection) or the four pages. The "frontend" CI
job only lints/typechecks/builds. Add Vitest + Testing Library for `lib/` and one Playwright happy
path (register → create project → upload → poll → open clip) — the backend already generates media
fixtures for exactly this.

### 22. Coverage is measured on the wrong half of the suite
`.github/workflows/ci.yml:89`

`pytest tests/unit --cov=app --cov-fail-under=70` computes coverage over unit tests only, while
`backend/app/api/**`, `services/auth.py`, `db/repositories.py` are exercised almost entirely by
`tests/integration`. The gate therefore measures a subset of the codebase and can pass while the
routes are 0% covered. Run coverage over the combined suite (unit + integration) in one job and
fail from there.

### 23. CI masks fixture failures and skips whole classes of check
`.github/workflows/ci.yml`

* `./ops/scripts/make_fixtures.sh || echo "fixtures script lands in Phase 2"` — a broken fixtures
  script becomes a green build, and media tests quietly skip. It also prints a stale "Phase 2"
  message. Remove the `||` fallback.
* `pip-audit … || true` makes the security job advisory-only, so a CVE never fails a PR.
* `ruff`, `mypy` and `bandit` run but nothing pins their versions, so a new lint release can turn
  the default branch red without a code change (especially with `ruff`'s yearly rule changes).
* `next lint` is deprecated in Next 15, there is **no ESLint config in `frontend/`**, and an
  interactive prompt in CI is a hang, not a failure.

### 24. Unpinned Python dependencies, no lockfile, no `.dockerignore`
`backend/requirements.txt` · `backend/requirements-dev.txt` · root (no `.dockerignore`)

`requirements.txt` uses lower bounds only (`fastapi>=0.115`, `torch`, `opencv-python-headless`,
`mediapipe` …). Two builds a week apart produce different images; a transitive bump in `torch` or
`numpy` will break `visual.py` with no diff to point at. Adopt `uv`/`pip-compile` lockfiles with
hashes, and pin CI tool versions. Without `.dockerignore`, every build ships `.git`, `node_modules`,
`models/` and test fixtures as build context.

### 25. Dev-compose ships production-shaped credentials and public ports
`docker-compose.yml:13-25,42-100`

Postgres (`autoclip/autoclip`), MinIO (`autoclip/autoclip123`) and the S3 keys are hardcoded
defaults, ports `5432`, `6379`, `9000/9001` are published on all interfaces, `minio/minio:latest`
is unpinned, and the API starts with `--reload` after running `alembic upgrade head` (two Gunicorn
workers in the Dockerfile, one reloader in compose — pick one model). This is the file people copy
into a staging host. Move defaults to `.env` with no fallback, bind infra ports to `127.0.0.1`, and
pin the MinIO image.

### 26. Documentation drift the code contradicts
`README.md`, `DEVELOPMENT_STATUS.md` §1/§3, `backend/app/main.py`, `frontend/README.md`

* `DEVELOPMENT_STATUS.md` says "194 passed", the suite has 233 tests; it still lists
  `backend/app/db/{models,migrations}` and `api/v1/` as "(empty — Phase 1)" in the tree, and
  `frontend/` as "structure only — Phase 1" while it is a working four-screen app.
* `README.md` advertises the 62-operation contract and a "Phase 0" backend while clips, rendering
  and analysis are implemented.
* `docs/SECURITY.md` should state the CSRF/cookie decision (#7) and the key-rotation status (#11)
  as they are, not as intended.
* A `docs/adr/` folder with one page per decision (sync SQLAlchemy, opaque sessions, eager Celery
  in tests, two contract files) would stop these three documents from re-rotating.

### 27. Repository hygiene
`backend/package-lock.json` (86 bytes, `name: backend`, empty `packages`) is an accidental
`npm install` artifact; `frontend/Dockerfile` sets `NEXT_PUBLIC_API_BASE: /api/proxy` in compose but
no such proxy route exists (the rewrite is `/api/v1/*`), so the variable is dead configuration; a
`CONTRIBUTING.md` with "how to run the stack in 5 commands" is missing and would have surfaced
issues #1–#4 on day one.

---

## Suggested order of attack

1. Fix #1, #2, #3, #4 — the stack boots and analysis works in containers (half a day).
2. Fix #6, #7, #9 — role string, CSRF bypass, real rate limiting (one day, security review gate).
3. Fix #13, #14 — readiness and the blocking upload path (one day; both are production incidents
   waiting to happen).
4. Add #21, #22, #23 — one frontend test, honest coverage, CI that fails (two days).
5. #19, #20, #25, #26 — contract single-source, schema drift check, compose hardening, docs refresh.

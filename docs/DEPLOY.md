# Deploying on a PaaS (no Docker locally)

Target: **Render / Railway / Fly.io / Heroku-style** platforms, running the **API and the workers**.
You do not need Docker installed; the platform builds `backend/Dockerfile` for you. Docker is only
the *build recipe* — it is the supported way to get FFmpeg, OpenCV's system libraries and a pinned
Python image onto the platform in one step.

If you would rather avoid Docker entirely, see [§8 Buildpack deploys](#8-buildpack-deploys-no-docker)
— it works, but you must install FFmpeg yourself and keep the two versions in sync.

---

## 1. What has to exist

| Component | Count | Why |
|---|---|---|
| **Web service** (API) | 1–N | `./scripts/start_api.sh`, binds `$PORT`, stateless |
| **Worker service** | 1–N | `./scripts/start_worker.sh`, consumes `media,transcribe,analyze` |
| **Beat** | **exactly 1** | `./scripts/start_beat.sh`, runs the stuck-job sweeper every 60 s |
| **Release hook** | once per deploy | `./scripts/release.sh`, migrations under a PostgreSQL advisory lock |
| **PostgreSQL** | 1 | must have `citext` **and** `vector` extensions (pgvector) |
| **Redis** | 1 | sessions, login throttle, rate limits, Celery broker + result backend |
| **Object storage** | 1 | S3-compatible (S3, R2, Neon storage, MinIO…) — **required in production** |
| **Persistent disk** | 1 (recommended) | `TMP_ROOT` scratch + `ASR_MODEL_DIR` weights + beat schedule |

The API and the workers are **different services from the same image**. On every platform below the
only difference is the start command.

> **The web service must not run workers.** A `WEB_CONCURRENCY`-scaled API that also ran Celery would
> process each job N times and block request handling behind a transcription.

---

## 2. Health checks and ports

* Port: **`$PORT`** — every platform injects it. `start_api.sh` binds it; nothing else needs changing.
* Health check path: **`/api/v1/health/readyz`**.
  It returns 503 until PostgreSQL, Redis, storage **and FFmpeg** all answer. That is the point: a
  service that cannot render video must not receive traffic.
  `/api/v1/health/livez` is liveness only (always 200) and must not be used as a readiness probe.
* First boot is slow: importing `torch`/`opencv`/`mediapipe` takes 10–30 s. Set the platform's
  initial health-check grace period to at least **60 s**.

---

## 3. Environment variables

Set these in the platform's dashboard. `SECRET_KEY`, `SECRET_ENCRYPTION_KEY` and `PASSWORD_PEPPER`
are **required** — the process refuses to boot without them and names what is missing.

```bash
# --- required ---
ENVIRONMENT=production
SECRET_KEY=<base64 of 32 random bytes>
SECRET_ENCRYPTION_KEY=<base64 of 32 random bytes>      # different from SECRET_KEY
PASSWORD_PEPPER=<at least 16 random characters>
DATABASE_URL=postgresql://user:pass@host/db             # platform's internal URL is fine
REDIS_URL=rediss://default:pass@host:6379               # managed Redis is usually TLS
SESSION_COOKIE_SECURE=true                              # required in production

# --- storage (required in production: local disk is refused) ---
STORAGE_BACKEND=s3
S3_BUCKET=autoclip-media
S3_REGION=auto                                          # R2: auto; AWS: your region
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com   # omit for AWS S3
S3_FORCE_PATH_STYLE=false                               # AWS/R2: false; MinIO: true

# --- deploy shape ---
WEB_CONCURRENCY=1                                       # 1 on ≤1 GB; one per vCPU otherwise
UVICORN_KEEP_ALIVE=75                                   # must exceed the proxy's idle timeout (~60 s)
FORWARDED_ALLOW_IPS=*                                   # behind the platform proxy
TRUSTED_HOSTS=api.yourdomain.com                        # not '*'
DOCS_ENABLED=false                                      # keep the API map private
TMP_ROOT=/data/tmp                                      # on the mounted disk
ASR_MODEL_DIR=/data/asr-models                          # on the mounted disk
MAX_UPLOAD_MB=512                                       # must fit the disk you mounted
CELERY_BEAT_SCHEDULE=/data/celerybeat-schedule

# --- worker shape ---
WORKER_QUEUES=media,transcribe,analyze
WORKER_CONCURRENCY=1                                    # 1–2 on a 1–2 GB instance
MAX_TASKS_PER_CHILD=20                                  # recycle after FFmpeg/OpenCV use
```

Generate the secrets locally:

```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # run twice
python -c "import secrets;print(secrets.token_urlsafe(32))"                     # pepper
```

Notes that bite on managed services:

* **Redis DB indexes.** Managed Redis usually allows only `/0`. Set `CELERY_BROKER_URL` /
  `CELERY_RESULT_BACKEND` explicitly (e.g. the same URL) instead of relying on `/1` and `/2`.
* **`DATABASE_URL`** may be `postgres://` or `postgresql://`; both are normalised automatically, and
  `DATABASE_URL_MIGRATIONS` (or Neon's `DATABASE_URL_UNPOOLED`) takes priority for migrations.
* **pgvector.** The database needs `citext` and `vector`. On Neon: `CREATE EXTENSION IF NOT EXISTS
  vector;` from the SQL editor. On Render/Railway Postgres images, check the extension list or use
  the `pgvector/pgvector:pg17` image if the platform lets you choose one.

---

## 4. Migrations

Run them **once per deploy, before the new instances take traffic** — never from the web process
(scaling to 2 replicas would run the same DDL twice):

```bash
./scripts/release.sh          # = python -m app.scripts.migrate
```

* It takes a PostgreSQL advisory lock, so concurrent releases serialise instead of deadlocking.
* It exits non-zero on failure, which must fail the deploy.
* `python -m app.scripts.migrate --check` prints `current=` / `head=` and changes nothing.

Platform wiring:

| Platform | How |
|---|---|
| Render | `preDeployCommand: ./scripts/release.sh` on the **web** service |
| Railway | a one-off service with start command `./scripts/release.sh`, or `railway run ./scripts/release.sh` |
| Fly | `[deploy] release_command = "/app/scripts/release.sh"` in `fly.toml` |
| Heroku | `release: cd backend && ./scripts/release.sh` in the `Procfile` |

The hook reads `DATABASE_URL_MIGRATIONS` (falling back to `DATABASE_URL`) straight from the
environment and needs nothing else — a missing Redis or S3 setting fails the **web** deploy, with
the message about that variable, instead of surfacing here as a confusing migration error.

`MIGRATE_ON_START=true` exists for a single-instance deploy (it runs the same script from
`start_api.sh`), but the release hook is the correct place once you run more than one replica.

---

## 5. Per-platform start commands

The build is always: **Dockerfile `backend/Dockerfile`**, context **repository root**, target
**`production`** (the default — it is the last stage).

| Service | Start / docker command |
|---|---|
| API (web) | `/app/scripts/start_api.sh` |
| Worker | `/app/scripts/start_worker.sh` |
| Beat | `/app/scripts/start_beat.sh` |
| Release | `/app/scripts/release.sh` |

**Render** — create a *Web Service* (Docker, Dockerfile path `backend/Dockerfile`, Docker context
`.`, health check `/api/v1/health/readyz`), a *Background Worker* with the same image and the worker
command, and a second background worker with `numInstances: 1` for beat. Add a persistent disk
mounted at `/data` to the API and worker services.

**Railway** — one project, three services from the same repo. Each service: builder "Dockerfile",
`dockerfilePath = backend/Dockerfile`, and a custom start command per the table above. Add the
Redis and Postgres plugins, and a volume mounted at `/data` on the worker service.

**Fly.io** — copy `deploy/fly.toml.example` to `fly.toml`, set the app name and region, then:

```bash
fly launch --no-deploy            # or: fly apps create <name>
fly volumes Create data --size 20 # persistent disk for TMP_ROOT / ASR models
fly secrets set SECRET_KEY=... SECRET_ENCRYPTION_KEY=... PASSWORD_PEPPER=... \
                DATABASE_URL=... REDIS_URL=... S3_...
fly deploy
fly scale count app=2 worker=1 beat=1
```

**Heroku** — `heroku container:push web worker beat --recursive`, then
`heroku container:release web worker beat`; add `release: cd backend && ./scripts/release.sh` to a
root `Procfile`. Heroku has no persistent disk: set `ASR_PROVIDER=openai` or accept the model
re-download on every dyno restart.

---

## 6. Disk, FFmpeg and the model cache

The three things that only fail on a PaaS:

1. **Scratch space.** Uploads spool to `TMP_ROOT` (bounded by `MAX_UPLOAD_MB`) and renders write
   their output there. An ephemeral disk is typically 512 MB–1 GB, so a 4 GB upload limit guarantees
   `ENOSPC`. Mount a disk and set `MAX_UPLOAD_MB` to something the disk can hold with headroom.
   The app now refuses to start if `TMP_ROOT` is unwritable, and warns when the free space is
   smaller than `MAX_UPLOAD_MB`.
2. **FFmpeg.** The Docker image installs it. A buildpack deploy does not — `/health/readyz` stays
   503 and every upload 503s with `media_tooling_unavailable`. This is the most common "it works
   locally" failure.
3. **Whisper weights.** `ASR_PROVIDER=local` downloads several GB into `ASR_MODEL_DIR` on first use.
   On ephemeral storage that repeats on every deploy (slow, and billed as egress). Put it on the
   disk, or use `ASR_PROVIDER=openai` and pay per minute of audio instead.

Startup logs a `app.preflight` warning for each of these, so check the deploy log rather than
guessing.

---

## 7. Frontend

Deploy `frontend/` as a separate service (it is a Next.js app, `output: standalone`).
Set `API_INTERNAL_URL` to the API's **internal** URL and keep the `/api/v1/*` rewrite: the session
cookie then stays first-party and CORS is not part of the auth story. Add the frontend origin to
`CORS_ALLOWED_ORIGINS` anyway (for direct API calls and the preflight requests the browser makes),
and set `PUBLIC_WEB_URL` / `PUBLIC_API_URL` to the real domains.

---

## 8. Buildpack deploys (no Docker)

If the platform cannot build a Dockerfile:

* Python buildpack + FFmpeg: Render/Railway honour a `nixpacks.toml`/`Aptfile`; Heroku needs the
  `ffmpeg` buildpack. Pin Python to **3.12** (`runtime.txt` / `PYTHON_VERSION`) — `cv2`, `mediapipe`
  and `ctranslate2` wheels are the constraint.
* Install from `backend/requirements.txt`, root directory `backend/`.
* Start commands: the same four scripts (§5). They `cd` to `backend/` themselves, so
  `backend/scripts/start_api.sh` is correct from a repository root checkout.
* Release command: `cd backend && ./scripts/release.sh`.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Service exits: `refusing to start: tmp_root_unusable` | `TMP_ROOT` is not writable (no disk mounted) | mount a disk at `/data`, set `TMP_ROOT=/data/tmp` |
| `refusing to start: s3_not_configured` | `STORAGE_BACKEND=s3` without credentials | set the three S3 variables |
| `refusing to start: ... SECRET_KEY ...` | missing/placeholder secret | generate real values (§3) |
| Health check 503, `ffmpeg: MISSING` | buildpack deploy without FFmpeg | deploy the Dockerfile, or install FFmpeg |
| Uploads 413 at 100 MB | platform body limit | increase it, or upload to S3 with a presigned URL (not implemented yet — `CODE_REVIEW.md`) |
| Uploads 503 `storage_unavailable` | S3 endpoint/credentials/region wrong | `S3_FORCE_PATH_STYLE=false` for AWS/R2, `true` for MinIO |
| Jobs stay `PENDING` forever | no worker service, or worker on a different `REDIS_URL` | check the worker log for `celery@… ready` and that both use the same broker |
| Jobs stuck `RUNNING` after a crash | beat not running | run exactly one beat; it sweeps after `JOB_STUCK_AFTER_SECONDS` |
| Every login rate-limited at once | `FORWARDED_ALLOW_IPS` not set | set `FORWARDED_ALLOW_IPS=*` so the real client IP is used |
| `400 Invalid host header` | `TRUSTED_HOSTS` set to an old domain | include the current API hostname |
| Migrations ran twice / deadlocked | migrations launched from the web process | use the release hook (§4) |
| Occasional `502` on the first request after an idle spell | `UVICORN_KEEP_ALIVE` shorter than the proxy's idle timeout: the server closed a connection the proxy still reuses | leave it at the 75 s default, or raise it above the platform's idle timeout |
| `... : uvicorn: not found` in the deploy log | the image/PATH has no console scripts (some buildpacks) | the scripts call `python -m uvicorn` / `python -m celery`; if `python` is not the interpreter, set `PYTHON_BIN=python3` |

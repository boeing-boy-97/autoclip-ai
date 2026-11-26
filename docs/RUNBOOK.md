# Deployment and operations runbook

## 1. Prerequisites

| Component | Version | Notes |
|---|---|---|
| Python | 3.12 | 3.13 works for most deps; `cv2`/`mediapipe` wheels are safest on 3.12 |
| Node | 20 LTS | Next.js 14/15 standalone output |
| PostgreSQL | 17 | `citext`, `pgcrypto` required; `vector` (pgvector) optional |
| Redis | 7/8 | broker, results, cache, SSE streams, locks |
| FFmpeg | 6.1+ | must include `libx264`, `aac`, `loudnorm`, `subtitles`, `freezedetect` |
| S3-compatible storage | R2 / S3 / MinIO | private bucket, versioning on, lifecycle rules |

Verify FFmpeg features before deploying:

```bash
ffmpeg -hide_banner -filters | grep -E 'subtitles|loudnorm|freezedetect|sidechaincompress'
ffmpeg -hide_banner -encoders | grep -E 'libx264|aac'
```

## 2. First-time setup

```bash
git clone <repo> && cd autoclip-ai
cp .env.example .env
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # run twice → SECRET_KEY, SECRET_ENCRYPTION_KEY
# edit .env: DATABASE_URL, REDIS_URL, storage, budgets
docker compose up --build            # dev: api, web, 7 workers, beat, postgres, redis, minio
```

Manual (no Docker):

```bash
cd backend
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
celery -A app.workers.celery_app worker -Q media,analyze -c 2 -n dev@%h --loglevel=INFO
celery -A app.workers.celery_app beat --loglevel=INFO
cd ../frontend && npm ci && npm run dev
```

## 3. Production topology

```
Internet → CDN/WAF → reverse proxy (TLS)
                      ├── /            → web (Next.js, ≥2 replicas)
                      ├── /api/proxy/* → api  (FastAPI, ≥2 replicas)   [server-side rewrite]
                      └── /metrics     → internal only
api + workers → managed Postgres (PITR) / Redis (AOF) / S3
beat → exactly ONE replica, `--lock` guarded
```

Health: `/api/v1/health/livez` (process up), `/readyz` (dependencies ready), `/health` (full report
including which integrations are **not configured**). Liveness must not depend on the DB; readiness must.

## 4. Scaling

* API and web are stateless → scale horizontally.
* `render` and `transcribe` are memory/CPU bound → scale per host, not per replica count. On a 2 vCPU
  / 2 GB host keep `WORKER_CONCURRENCY_RENDER=1` and `..._TRANSCRIBE=1`.
* Watch `queue_depth{queue}` and `job_duration_seconds`; add workers for the queue with growing depth
  rather than scaling everything.
* `publish` stays at 1–2 workers to respect YouTube quota.
* Celery `--max-tasks-per-child` is set per queue to bound FFmpeg/OpenCV memory fragmentation.

## 5. Backup and restore

* Postgres: automated daily snapshot + continuous WAL → PITR. **Restore drill quarterly**:
  restore to a scratch instance, `alembic upgrade head`, run `verify_schema.sh`, smoke-test login.
* Redis: AOF on. Losing Redis is survivable (broker/result/cache) but in-flight jobs must be
  re-enqueued by the stuck-job sweeper — verify this in the drill.
* Object storage: versioning on; lifecycle rules only for derived assets, never for
  `source_video`/`clip_video` keys without explicit user deletion.

## 6. Common incidents

### Queue is backing up
1. `GET /api/v1/health` → check Redis + DB. 2. Check `queue_depth` per queue. 3. Check worker logs for
crash loops (`worker.heartbeat_missed`). 4. Verify disk: render refuses at `MIN_FREE_DISK_GB`.
5. Scale the specific queue. 6. Dead-lettered jobs appear in Processing Queue with a Retry button.

### Stuck job (RUNNING forever)
The sweeper fails jobs whose `heartbeat_at` is older than `2 × time_limit`. Manual path:
`POST /api/v1/jobs/{id}/cancel` then `/retry`. Because stages are checkpointed, retry resumes from the
last successful stage — it does **not** re-run transcription or re-pay for analysis.

### A/V or caption failure on one clip
`POST /api/v1/clips/{id}/quality-check` with `auto_repair=true`. Repair is bounded (2 attempts per
check, 3 versions). If it still fails, the clip lands in `AWAITING_REVIEW` naming the failing check —
it is not publishable and will not be published by the auto path.

### YouTube quota exhausted
Symptom: `quota_exhausted`, publish jobs re-queued to next reset (00:00 America/Los_Angeles for the
Google project). Verify `youtube_quota_units_remaining`; reduce publishing cadence; request a quota
increase from Google. Uploads are idempotent — the retry will not duplicate a video.

### OAuth token revoked / expired permanently
Channel flips to `token_expired`, notification `auth_expired`, publishing blocked for that channel.
User reconnects via OAuth; existing jobs stay queued. `invalid_grant` never results in a silent
retry loop — after 3 failures the channel is marked `error` and requires human action.

### Upload succeeded but job shows failed
Do **not** retry blindly. The recovery path looks up `publishing_jobs.youtube_video_id` (persisted
before any retry decision) or the `autoclip-id:<job_id>` correlation tag in recent uploads, and adopts
the existing video. Forcing a re-upload from the UI requires explicit confirmation because it can
create a duplicate.

### Disk full
Renders fail fast with `storage.low_disk`. Clean `$TMP_ROOT/*` (safe: per-job dirs are disposable),
verify the sweeper removed orphans, then resume jobs.

### Migration went wrong
Never edit an applied migration. Restore to PITR before the deploy, fix forward with a new revision,
and re-run `verify_schema.sh` to confirm invariants still hold.

## 7. Release checklist

1. CI green (lint, types, unit, integration, contract, E2E, security).
2. `alembic upgrade head` tested on a **copy of production data**.
3. `ops/scripts/verify_schema.sh` passes.
4. `ops/scripts/validate_openapi.py` passes and `docs/openapi.json` is committed (no drift).
5. New env vars added to `.env.example` **and** documented here / in `DEVELOPMENT_STATUS.md`.
6. Queue concurrency reviewed for the target host size.
7. Rollback plan: keep the previous image tag; migrations are forward-only but additive for one release.
8. `DEVELOPMENT_STATUS.md` updated with what shipped and what remains.

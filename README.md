# AutoClipper AI

Turn one authorized long-form video into multiple professionally edited vertical shorts — with real
video processing, a multimodal analysis pipeline, an independent quality-assurance stage, and official
YouTube publishing.

> **Status: implemented clipping workflow; production verification still required.** Authentication (opaque sessions, CSRF, rate limits),
> workspaces and roles, project state machine, source ingestion with real FFprobe validation,
> background analysis (Celery) with job control, clip generation and 9:16 rendering with burned-in
> captions all run today; publishing is designed but not implemented. See
> [`DEVELOPMENT_STATUS.md`](DEVELOPMENT_STATUS.md) for the per-phase detail, and
> [`CODE_REVIEW.md`](CODE_REVIEW.md) for the known gaps and their priority.

---

## Verify the current state yourself

```bash
./ops/scripts/phase0_verify.sh
```

This applies the schema to a real PostgreSQL instance, proves 16 integrity invariants, validates the
OpenAPI 3.1 contract, checks every config file and template, validates the Compose topology, asserts
`.env.example` parity with the settings model, runs the unit tests, and scans production code for fake
behaviour. It exits non-zero if anything fails.

Individual checks:

```bash
./ops/scripts/verify_schema.sh          # schema applies + invariants hold
python3 ops/scripts/validate_openapi.py # contract correctness
cd backend && python3 -m pytest tests/unit
```

---

## Quick start

```bash
./ops/scripts/dev_env.sh          # writes .env with generated SECRET_KEY / SECRET_ENCRYPTION_KEY / PASSWORD_PEPPER
docker compose up --build
```

`cp .env.example .env` is not enough: the settings model rejects placeholder secrets at boot, in
every environment, on purpose. The script generates the three values that cannot be defaulted.

Run the API without Docker:

```bash
cd backend
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload            # http://localhost:8000/docs
celery -A app.worker.celery_app worker -Q media,transcribe,analyze -l INFO
celery -A app.worker.celery_app beat -l INFO   # runs the stuck-job sweeper
```

| Service | URL |
|---|---|
| Web app | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| API readiness | http://localhost:8000/api/v1/health/readyz |
| API health report | http://localhost:8000/api/v1/health |
| MinIO console | http://localhost:9001 |

The API refuses to boot on missing/invalid configuration and names the offending variable. Optional
integrations (YouTube, paid AI) are reported as **not configured** rather than faked.

---

## Product workflow (implemented and planned)

```
SUBMIT VIDEO → UNDERSTAND → FIND MOMENTS → RANK → GENERATE CLIPS → EDIT → CHECK QUALITY
             → PREPARE METADATA → PUBLISH → LEARN FROM PERFORMANCE
```

1. **Ingest & inspect** — FFprobe validation, SHA-256, duration/codec/stream facts persisted.
2. **Understand** — audio extraction, word-level ASR, scene detection, salience-sampled vision, then a
   chunked, schema-validated semantic analysis (never the whole video re-sent to a model).
3. **Discover moments** — the model proposes ideas; deterministic code snaps boundaries to real word
   onsets, enforces completeness, and scores on ten weighted criteria rolled into four displayed
   sub-scores. Overlap and semantic dedupe preserve diversity.
4. **Produce** — a declarative `RenderPlan` compiles to FFmpeg: subject-aware 9:16 reframing with crop
   smoothing, ASS captions from word timings, LUFS-normalised audio, 8 editing templates.
5. **Check quality** — a separate service re-inspects the *file*: technical (black/frozen frames, A/V
   sync, resolution), editorial, duplicate, and safety indicators. Bounded auto-repair fixes specific
   failures; anything unresolved goes to human review and is never publishable.
6. **Publish** — official YouTube OAuth + resumable upload, idempotent per (clip version, channel),
   persistent scheduler, quota-aware.
7. **Learn** — YouTube Analytics sync feeds measured performance back into selection, with sample-size
   caveats and no causal claims.

Steps 5–7 describe the target architecture, not completed capabilities. Current rendering uses a
center crop, not subject tracking. Rendered/restored clips remain `AWAITING_REVIEW` and are not
automatically publishable: independent QA, approval, publishing, and analytics are not implemented.
Authenticated preview and MP4 download work through the media route for local storage or S3.

Design details, including the state machine, data model invariants and failure-recovery model:
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Principles the code follows

* **Real functionality before decoration.** No analytics cards without database rows behind them.
* **Accuracy before speed; context before arbitrary durations.** A 38-second complete idea beats a
  30-second clip that cuts off the conclusion.
* **Human approval when confidence is insufficient.** Auto-approval is off by default.
* **Fail loudly, recover cheaply.** Stages checkpoint, so a render failure never re-pays for analysis
  and a publish failure never regenerates video.
* **No fake integrations.** Unconfigured services report exactly which env vars are missing.
* **Authorized media only.** A rights declaration is required before ingestion; a YouTube URL is not a
  download grant. Copyright signals are risk indicators, never legal verdicts.
* **Virality is not promised.** Scores are labeled as estimates of content characteristics.
* **Diversity, not volume.** Near-duplicate clips are suppressed rather than shipped.

---

## Repository layout

```
backend/    FastAPI, Celery workers, FFmpeg/transcription/analysis/clip pipeline
frontend/   Next.js app: auth, dashboard, projects, jobs, clip preview/download
config/     scoring weights, analysis tuning, pricing, editing templates
docs/       architecture, diagrams, OpenAPI contract, logging/testing/security/runbook
ops/        verification and schema scripts, monitoring
storage/    local dev media root (not for production)
```

## Requirements

Python 3.12 · Node 20 · PostgreSQL 17 (with `pgvector` optional) · Redis 7/8 · FFmpeg 6.1+
(`libx264`, `aac`, `loudnorm`, `subtitles`, `freezedetect`, `sidechaincompress`) · S3-compatible
storage for production.

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | System design, data model, pipeline, queues, security |
| [diagrams](docs/diagrams/README.md) | Container view, state machine, render/QA loop, publish sequence |
| [openapi.json](docs/openapi.json) | 62-operation API contract (OpenAPI 3.1, validated) |
| [TESTING.md](docs/TESTING.md) | Test layers, fixtures, coverage gates |
| [SECURITY.md](docs/SECURITY.md) | AuthN/Z, uploads, SSRF, secrets, compliance |
| [LOGGING.md](docs/LOGGING.md) | Log schema, redaction, metrics |
| [RUNBOOK.md](docs/RUNBOOK.md) | Deployment, scaling, incident response |
| [DEVELOPMENT_STATUS.md](DEVELOPMENT_STATUS.md) | Phase progress, verified results, limitations, next phase |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Deploying to a PaaS (Render / Railway / Fly / Heroku) with workers |
| [CODE_REVIEW.md](CODE_REVIEW.md) | Known gaps, ranked by priority |

## License

Proprietary. All rights reserved.

## Frontend development and quality checks

Use Node 22.12+ (or Node 20.19+). From the repository root:

```bash
npm ci --prefix frontend
npm --prefix frontend run dev -- --hostname 0.0.0.0
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build
npm --prefix frontend start -- --hostname 0.0.0.0
```

The browser uses same-origin `/api/v1` requests. Set `API_INTERNAL_URL` **before building** the
frontend when the backend is not at `http://127.0.0.1:8000`. The Docker build defaults to
`http://api:8000`; override with `--build-arg API_INTERNAL_URL=...` for another internal host.
A runtime-only environment change does not rebuild Next.js rewrites.

See [the engineering audit](docs/ENGINEERING_AUDIT.md) for implemented repairs, actual test
results, and outstanding production gates. The older status/review documents are historical.

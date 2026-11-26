# AutoClipper AI

Turn one authorized long-form video into multiple professionally edited vertical shorts — with real
video processing, a multimodal analysis pipeline, an independent quality-assurance stage, and official
YouTube publishing.

> **Status: Phase 0 (architecture & foundation) complete.** The design, database schema, API contract,
> environment, Docker topology and a minimal tested backend scaffold exist. Domain features land
> phase by phase — see [`DEVELOPMENT_STATUS.md`](DEVELOPMENT_STATUS.md).

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
cp .env.example .env
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"   # twice: SECRET_KEY, SECRET_ENCRYPTION_KEY
# edit .env → set those two values plus PASSWORD_PEPPER
docker compose up --build
```

| Service | URL |
|---|---|
| Web app | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| API health | http://localhost:8000/api/v1/health |
| MinIO console | http://localhost:9001 |

The API refuses to boot on missing/invalid configuration and names the offending variable. Optional
integrations (YouTube, paid AI) are reported as **not configured** rather than faked.

---

## How it works

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
backend/    FastAPI, Celery workers, FFmpeg/AI pipeline, QA, publishing
frontend/   Next.js app (scaffold; Phase 1)
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

## License

Proprietary. All rights reserved.

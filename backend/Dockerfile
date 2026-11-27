# syntax=docker/dockerfile:1.7
# =====================================================================================
# Backend image: FastAPI + Celery workers + FFmpeg + OpenCV/MediaPipe runtime deps.
# Same image runs the API and every worker (different commands), which keeps versions
# identical between them.
#
# Stages
#   base        shared system + Python dependencies
#   dev         adds test/quality tooling, uvicorn --reload (used by docker-compose)
#   production  the LAST stage: what a PaaS builds when you point it at this Dockerfile
#               without a target. It must stay last — while `dev` was last, every cloud
#               build silently deployed a --reload server.
#
# Process types (start commands, one service each):
#   api     ./scripts/start_api.sh        binds $PORT, honours WEB_CONCURRENCY
#   worker  ./scripts/start_worker.sh     consumes media,transcribe,analyze
#   beat    ./scripts/start_beat.sh       exactly one instance
#   release ./scripts/release.sh          migrations (pre-deploy hook, not a service)
# See docs/DEPLOY.md.
# =====================================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMP_ROOT=/tmp/autoclip \
    DEBIAN_FRONTEND=noninteractive

# FFmpeg (rendering), plus OpenCV/MediaPipe headless runtime libs.
# `curl` is the readiness probe; `tini` reaps zombie FFmpeg processes.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libgomp1 \
      curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- dependency layer (cached independently of source) ---
COPY backend/requirements.txt backend/requirements-dev.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- source + runtime entrypoints ---
COPY backend/ /app/
COPY config/ /app/config/
RUN mkdir -p /tmp/autoclip /app/storage && \
    chmod +x /app/scripts/*.sh && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app /tmp/autoclip

USER appuser

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]

# --------------------------------------------------------------------------- dev
FROM base AS dev
USER root
RUN pip install --no-cache-dir -r /app/requirements-dev.txt
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# --------------------------------------------------------------------------- production
# Readiness, not liveness: a container that answers HTTP but cannot reach PostgreSQL, Redis or
# FFmpeg is not healthy. Worker services ignore/disable this probe — they serve no HTTP.
FROM base AS production
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS "http://localhost:${PORT:-8000}/api/v1/health/readyz" || exit 1
CMD ["/app/scripts/start_api.sh"]

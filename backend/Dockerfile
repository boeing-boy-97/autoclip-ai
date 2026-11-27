# syntax=docker/dockerfile:1.7
# =====================================================================================
# Backend image: FastAPI + Celery workers + FFmpeg + OpenCV/MediaPipe runtime deps.
# Same image runs api and all workers (different commands), which keeps versions identical.
# =====================================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMP_ROOT=/tmp/autoclip \
    DEBIAN_FRONTEND=noninteractive

# FFmpeg (rendering), plus OpenCV/MediaPipe headless runtime libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libgomp1 \
      curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- dependency layer (cached independently of source) ---
COPY backend/requirements.txt backend/requirements-dev.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# --- source ---
COPY backend/ /app/
COPY config/ /app/config/
RUN mkdir -p /tmp/autoclip /app/storage && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app /tmp/autoclip

USER appuser

EXPOSE 8000
# Readiness, not liveness: a container that answers HTTP but cannot reach PostgreSQL, Redis or
# FFmpeg is not healthy. Worker containers disable this check in docker-compose.yml — they serve
# no HTTP, so an HTTP probe there would report a running worker as unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/v1/health/readyz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", "--bind", "0.0.0.0:8000", \
     "--timeout", "120", "--graceful-timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-"]

# ---------------------------------------------------------------------------
FROM base AS dev
USER root
RUN pip install --no-cache-dir -r /app/requirements-dev.txt
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

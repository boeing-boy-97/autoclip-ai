#!/usr/bin/env bash
# =====================================================================================
# Celery worker process for a PaaS. Run this as its OWN service — never inside the API
# process, and never with more than one `beat` anywhere.
#
#   WORKER_QUEUES        comma-separated queues to consume (default media,transcribe,analyze)
#   WORKER_CONCURRENCY   prefork processes (default 2). Transcription is memory-hungry:
#                        on a 1 GB instance use 1, or the OS OOM-kills the worker mid-job.
#   WORKER_NAME          node name prefix, so logs show which service did the work
#   MAX_TASKS_PER_CHILD  recycle workers to bound FFmpeg/OpenCV memory fragmentation
#   PYTHON_BIN           interpreter to run (default `python`, same as start_api.sh)
# =====================================================================================
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # -> backend/

QUEUES="${WORKER_QUEUES:-media,transcribe,analyze}"
CONCURRENCY="${WORKER_CONCURRENCY:-2}"
NAME="${WORKER_NAME:-worker}"
PYTHON="${PYTHON_BIN:-python}"

echo "worker: queues=${QUEUES} concurrency=${CONCURRENCY}"

exec "${PYTHON}" -m celery -A app.worker.celery_app worker \
  -Q "${QUEUES}" \
  -c "${CONCURRENCY}" \
  -n "${NAME}@%h" \
  --max-tasks-per-child="${MAX_TASKS_PER_CHILD:-50}" \
  --loglevel="${CELERY_LOG_LEVEL:-INFO}"

#!/usr/bin/env bash
# =====================================================================================
# Celery beat — exactly ONE instance per deployment, ever. Two beat schedulers double-fire
# the stuck-job sweeper; it is idempotent, but the duplicate timers are pointless noise.
#
#   CELERY_BEAT_SCHEDULE  path of the schedule db (default $TMP_ROOT/celerybeat-schedule).
#                         Put it on the persistent disk: on ephemeral storage a restart
#                         forgets the schedule and re-fires immediately.
# =====================================================================================
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # -> backend/

export AUTOCLIP_PROCESS="beat"

PYTHON="${PYTHON_BIN:-python}"
SCHEDULE="${CELERY_BEAT_SCHEDULE:-${TMP_ROOT:-/tmp/autoclip}/celerybeat-schedule}"
mkdir -p "$(dirname "${SCHEDULE}")"

echo "beat: schedule=${SCHEDULE}"

exec "${PYTHON}" -m celery -A app.worker.celery_app beat \
  --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
  --schedule "${SCHEDULE}"

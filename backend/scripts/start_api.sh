#!/usr/bin/env bash
# =====================================================================================
# API process for a PaaS (Render / Railway / Fly.io / Heroku) — or anywhere without compose.
#
#   PORT               bind port (every PaaS injects this; default 8000)
#   WEB_CONCURRENCY    uvicorn worker processes (default 1). Raise it to the instance's vCPU
#                      count; each worker holds its own copy of the app, so on a 512 MB plan
#                      more than one gets OOM-killed.
#   UVICORN_KEEP_ALIVE seconds to keep an idle client connection (default 75). This MUST be
#                      longer than the platform proxy's idle timeout (~60 s on most), otherwise
#                      the server closes a connection the proxy still believes is open and the
#                      next request on it becomes a 502.
#   PYTHON_BIN         interpreter to run (default `python`; set python3 if that is what exists)
#   FORWARDED_ALLOW_IPS  which proxies may set X-Forwarded-* (default 127.0.0.1).
#                        On a PaaS set "*": the platform's proxy is not on localhost, and
#                        without this every request appears to come from the proxy's IP —
#                        which collapses the login rate limit into one shared bucket and
#                        writes the proxy IP into audit_logs.
#   MIGRATE_ON_START   "true" runs `python -m app.scripts.migrate` first. Convenient on a
#                      single instance; use the platform's release/pre-deploy hook instead
#                      once you run more than one replica (see docs/DEPLOY.md).
# =====================================================================================
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # -> backend/

# Tells app/core/preflight.py which checks apply to this process (see the module docstring).
export AUTOCLIP_PROCESS="api"

PORT="${PORT:-8000}"
WORKERS="${WEB_CONCURRENCY:-1}"
PYTHON="${PYTHON_BIN:-python}"

if [[ "${MIGRATE_ON_START:-false}" == "true" ]]; then
  echo "release: applying migrations"
  "${PYTHON}" -m app.scripts.migrate
fi

echo "api: listening on 0.0.0.0:${PORT} with ${WORKERS} worker(s)"

exec "${PYTHON}" -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --workers "${WORKERS}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
  --timeout-keep-alive "${UVICORN_KEEP_ALIVE:-75}" \
  --timeout-graceful-shutdown 30 \
  --no-server-header \
  --no-access-log

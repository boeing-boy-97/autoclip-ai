#!/usr/bin/env bash
# =====================================================================================
# Release / pre-deploy hook: apply database migrations once, then exit.
#
# Wire this into the platform's release phase so it runs BEFORE new instances serve
# requests — not from the web process, where N replicas would race each other:
#   Render   → preDeployCommand
#   Railway  → deploy command of a one-off service, or `railway run`
#   Fly      → `fly deploy --pre-deploy-command` / release_command in fly.toml
#   Heroku   → `release:` entry in the Procfile
#
# Safe to run repeatedly: it takes a PostgreSQL advisory lock and `upgrade head` is a no-op
# when the schema is current. Exits non-zero on failure, which aborts the deploy.
# =====================================================================================
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # -> backend/

exec "${PYTHON_BIN:-python}" -m app.scripts.migrate "$@"

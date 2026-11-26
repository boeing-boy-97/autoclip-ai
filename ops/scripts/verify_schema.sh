#!/usr/bin/env bash
# Verify the Phase 0 database schema applies cleanly and enforces its invariants.
# Usage: ops/scripts/verify_schema.sh [--keep]
# Requires a reachable PostgreSQL with superuser-ish rights (uses the `postgres` role by
# default; override with PGSUPERUSER / PGPORT).
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA="${REPO_ROOT}/docs/database/schema.sql"
INVARIANTS="${REPO_ROOT}/docs/database/verify_invariants.sql"
DB="${SCHEMA_TEST_DB:-autoclip_schema_test}"
PGBIN="${PGBIN:-/usr/lib/postgresql/17/bin}"
PSQL="${PGBIN}/psql -p ${PGPORT:-5432}"
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

run_pg() { sudo -n -u postgres ${PSQL} "$@"; }

echo "==> dropping and recreating ${DB}"
run_pg -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null
run_pg -c "CREATE DATABASE ${DB};" >/dev/null

# Optional extensions the schema assumes (see notes at the bottom of schema.sql).
run_pg -d "${DB}" -c "CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS vector;" \
  >/dev/null 2>&1 || echo "!! pgvector/citext unavailable — some columns will be skipped"

echo "==> applying schema.sql"
# Feed the file on stdin: the postgres OS user cannot read paths under /home/user.
APPLY_OUT="$(run_pg -d "${DB}" -v ON_ERROR_STOP=1 < "${SCHEMA}" 2>&1)" || true
if echo "${APPLY_OUT}" | grep -Eqi '(^| )ERROR:|FATAL:'; then
  echo "${APPLY_OUT}" | grep -Ei 'error|fatal' >&2
  echo "!! schema application FAILED" >&2; exit 1
fi
echo "==> schema applied with no errors"

echo "==> object inventory"
run_pg -d "${DB}" -t -c "
  SELECT 'tables:  '||count(*) FROM information_schema.tables WHERE table_schema='public'
  UNION ALL SELECT 'indexes: '||count(*) FROM pg_indexes WHERE schemaname='public'
  UNION ALL SELECT 'fks:     '||count(*) FROM pg_constraint WHERE contype='f'
  UNION ALL SELECT 'checks:  '||count(*) FROM pg_constraint WHERE contype='c'
  UNION ALL SELECT 'enums:   '||count(*) FROM pg_type WHERE typtype='e';"

echo "==> verifying invariants"
OUT="$(run_pg -d "${DB}" < "${INVARIANTS}" 2>&1)"
echo "${OUT}" | grep -E 'PASS:|FAIL:' | sed -E 's/^(psql:[^ ]*|NOTICE:  )//'

PASS=$(echo "${OUT}" | grep -c 'PASS:' || true)
FAIL=$(echo "${OUT}" | grep -c 'FAIL:' || true)
echo "==> ${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -ne 0 || "${PASS}" -eq 0 ]]; then
  echo "!! invariant verification FAILED" >&2; exit 1
fi

if [[ "${KEEP}" -eq 0 ]]; then
  run_pg -c "DROP DATABASE IF EXISTS ${DB};" >/dev/null
  echo "==> dropped test database"
fi
echo "✅ schema OK"

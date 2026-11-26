#!/usr/bin/env bash
# =====================================================================================
# Schema drift check.
#
# Proves that `alembic upgrade head` produces EXACTLY the schema in docs/database/schema.sql.
# Two fresh databases are built — one by migration, one by applying schema.sql — and their
# `pg_dump --schema-only` output is diffed. Anything other than the expected noise (alembic's
# own bookkeeping table and pg_dump's randomised \restrict tokens) fails the build.
#
# Usage: ops/scripts/verify_migrations.sh [--keep]
# =====================================================================================
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PGBIN="${PGBIN:-/usr/lib/postgresql/17/bin}"
PGPORT="${PGPORT:-5432}"
DB_MIG="${DB_MIG:-autoclip_drift_migrated}"
DB_REF="${DB_REF:-autoclip_drift_reference}"
PSQL_SUPER="sudo -n -u postgres ${PGBIN}/psql -p ${PGPORT}"
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

log() { printf '\033[1m▶ %s\033[0m\n' "$1"; }
ok()  { printf '  \033[32m✓ %s\033[0m\n' "$1"; }
bad() { printf '  \033[31m✗ %s\033[0m\n' "$1"; }

log "creating two empty databases"
${PSQL_SUPER} -c "DROP DATABASE IF EXISTS ${DB_MIG} WITH (FORCE);" >/dev/null
${PSQL_SUPER} -c "DROP DATABASE IF EXISTS ${DB_REF} WITH (FORCE);" >/dev/null
${PSQL_SUPER} -c "CREATE DATABASE ${DB_MIG} OWNER autoclip;" >/dev/null
${PSQL_SUPER} -c "CREATE DATABASE ${DB_REF} OWNER autoclip;" >/dev/null

# Reference DB: extensions then schema.sql verbatim (as a superuser, so extensions install).
${PSQL_SUPER} -d "${DB_REF}" -q \
  -c "CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgcrypto;" >/dev/null
if ! ${PSQL_SUPER} -d "${DB_REF}" -v ON_ERROR_STOP=1 -q < "${REPO_ROOT}/docs/database/schema.sql" 2>&1 | grep -Ei 'error|fatal'; then
  ok "schema.sql applied to reference database"
else
  bad "schema.sql failed to apply"; exit 1
fi

log "applying alembic migrations"
cd "${REPO_ROOT}/backend"
# Alembic needs application settings; supply throwaway values for anything unused.
export SECRET_KEY="${SECRET_KEY:-$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')}"
export SECRET_ENCRYPTION_KEY="${SECRET_ENCRYPTION_KEY:-$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')}"
export PASSWORD_PEPPER="${PASSWORD_PEPPER:-drift-check-pepper-value-1234}"
export DATABASE_URL="postgresql+psycopg://autoclip:autoclip@127.0.0.1:${PGPORT}/${DB_MIG}"
export REDIS_URL="redis://127.0.0.1:6379/0"
export STORAGE_BACKEND=local
if python3 -m alembic upgrade head >/tmp/drift_upgrade.log 2>&1; then
  ok "alembic upgrade head succeeded"
else
  bad "alembic upgrade failed"; tail -25 /tmp/drift_upgrade.log; exit 1
fi

log "round-trip: downgrade base then upgrade head again"
if python3 -m alembic downgrade base >/tmp/drift_down.log 2>&1; then
  remaining=$(${PSQL_SUPER} -d "${DB_MIG}" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
  # alembic_version is recreated/kept by alembic itself; 0 or 1 is acceptable.
  if [[ "${remaining}" -le 1 ]]; then ok "downgrade removed all domain tables (${remaining} bookkeeping table left)";
  else bad "downgrade left ${remaining} tables behind"; exit 1; fi
else
  bad "alembic downgrade failed"; tail -20 /tmp/drift_down.log; exit 1
fi
if python3 -m alembic upgrade head >/tmp/drift_up2.log 2>&1; then ok "re-upgrade succeeded"; else
  bad "re-upgrade failed"; tail -20 /tmp/drift_up2.log; exit 1; fi

log "diffing DDL: migrated database vs schema.sql database"
dump() {
  sudo -n -u postgres ${PGBIN}/pg_dump -p "${PGPORT}" --schema-only --no-owner --no-privileges \
    --schema=public "$1" 2>/dev/null \
  | grep -vE '^(--|SET |SELECT pg_catalog|$)' \
  | grep -vE '^\\(un)?restrict '
}
dump "${DB_MIG}" > /tmp/drift_migrated.sql
dump "${DB_REF}" > /tmp/drift_reference.sql

# The alembic_version table body is emitted on lines of its own; strip the whole block.
strip_alembic() {
  python3 - "$1" <<'PY'
import re, sys

sql = open(sys.argv[1]).read()
# Alembic's bookkeeping table is the only thing not described by schema.sql. Remove its
# CREATE TABLE block with a line-based scan (robust to pg_dump's column wrapping).
lines, out, skip = sql.splitlines(), [], False
for line in lines:
    if re.search(r"alembic_version\s*\(", line):
        skip = True
        continue
    if skip:
        if line.strip().startswith(");"):
            skip = False
        continue
    if "alembic_version" in line:
        continue
    out.append(line)
print("\n".join(out))
PY
}

strip_alembic /tmp/drift_migrated.sql > /tmp/drift_migrated_clean.sql
strip_alembic /tmp/drift_reference.sql > /tmp/drift_reference_clean.sql

echo "  migrated DDL lines: $(wc -l < /tmp/drift_migrated_clean.sql) | schema.sql DDL lines: $(wc -l < /tmp/drift_reference_clean.sql)"
if diff -u /tmp/drift_reference_clean.sql /tmp/drift_migrated_clean.sql > /tmp/drift_diff.txt; then
  ok "NO DRIFT — migrations reproduce schema.sql exactly"
else
  bad "schema drift detected:"
  head -40 /tmp/drift_diff.txt
  exit 1
fi

if [[ ${KEEP} -eq 0 ]]; then
  ${PSQL_SUPER} -c "DROP DATABASE IF EXISTS ${DB_MIG} WITH (FORCE);" >/dev/null
  ${PSQL_SUPER} -c "DROP DATABASE IF EXISTS ${DB_REF} WITH (FORCE);" >/dev/null
  echo "  dropped scratch databases"
fi
printf '\n\033[32m✅ migrations verified\033[0m\n'

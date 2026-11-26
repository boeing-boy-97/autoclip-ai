#!/usr/bin/env bash
# =====================================================================================
# Phase 0 verification — runs every check that Phase 0 claims to satisfy.
# Each step either passes or fails loudly; nothing is skipped silently.
#
# Usage: ops/scripts/phase0_verify.sh
# Requires: python3, psql (or the local postgres superuser), ffmpeg for later phases.
# =====================================================================================
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

PASS=0; FAIL=0
step() { printf '\n\033[1m▶ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓ %s\033[0m\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗ %s\033[0m\n' "$1"; FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------- 1. documents
step "1. Required Phase 0 documents exist and are non-trivial"
declare -A DOCS=(
  [docs/ARCHITECTURE.md]=5000
  [docs/database/schema.sql]=5000
  [docs/openapi.json]=10000
  [docs/LOGGING.md]=1500
  [docs/TESTING.md]=1500
  [docs/SECURITY.md]=1500
  [docs/RUNBOOK.md]=1500
  [.env.example]=2000
  [docker-compose.yml]=1500
  [config/scoring.yaml]=400
  [config/analysis.yaml]=400
  [config/pricing.yaml]=300
)
for f in "${!DOCS[@]}"; do
  if [[ -f "$f" && $(wc -c < "$f") -ge ${DOCS[$f]} ]]; then
    ok "$f ($(wc -c < "$f") bytes)"
  else
    bad "$f missing or too small"
  fi
done

# ---------------------------------------------------------------- 2. schema
step "2. Database schema applies cleanly and enforces its invariants"
if ./ops/scripts/verify_schema.sh >/tmp/phase0_schema.log 2>&1; then
  ok "schema + $(grep -c 'PASS:' /tmp/phase0_schema.log) invariants verified"
else
  bad "schema verification failed (see /tmp/phase0_schema.log)"
  tail -20 /tmp/phase0_schema.log
fi

# ---------------------------------------------------------------- 3. API contract
step "3. API contract is valid OpenAPI 3.1 and internally consistent"
if python3 ops/scripts/validate_openapi.py >/tmp/phase0_openapi.log 2>&1; then
  ok "$(tail -1 /tmp/phase0_openapi.log)"
else
  bad "openapi validation failed"; cat /tmp/phase0_openapi.log
fi

# ---------------------------------------------------------------- 4. config files
step "4. Configuration files parse and obey their own rules"
python3 - <<'PY'
import sys, glob, yaml

fail = 0
scoring = yaml.safe_load(open("config/scoring.yaml"))
total = sum(c["weight"] for c in scoring["criteria"].values())
if abs(total - 1.0) < 1e-9:
    print(f"  \033[32m✓ scoring weights sum to 1.0 ({len(scoring['criteria'])} criteria)\033[0m")
else:
    print(f"  \033[31m✗ scoring weights sum to {total}, expected 1.0\033[0m"); fail += 1

rollup = set(scoring["rollup"])
crit = set(scoring["criteria"])
if rollup == {"content_potential", "technical_quality", "context_completeness", "audience_relevance"}:
    print("  \033[32m✓ four decomposed sub-scores declared\033[0m")
else:
    print("  \033[31m✗ sub-score rollup incomplete\033[0m"); fail += 1
bad_refs = {c for v in scoring["rollup"].values() for c in v} - crit
if not bad_refs:
    print("  \033[32m✓ rollup references only declared criteria\033[0m")
else:
    print(f"  \033[31m✗ rollup references unknown criteria: {bad_refs}\033[0m"); fail += 1

yaml.safe_load(open("config/analysis.yaml"))
yaml.safe_load(open("config/pricing.yaml"))
print("  \033[32m✓ analysis.yaml and pricing.yaml parse\033[0m")

for f in sorted(glob.glob("config/templates/*.yaml")):
    t = yaml.safe_load(open(f))
    name = f.split("/")[-1]
    assert t["canvas"]["width"] == 1080 and t["canvas"]["height"] == 1920, name
    for key in ("reframe", "captions", "audio", "branding"):
        assert key in t, f"{name} missing {key}"
print(f"  \033[32m✓ {len(glob.glob('config/templates/*.yaml'))} editing templates valid, all 1080x1920\033[0m")

pricing = yaml.safe_load(open("config/pricing.yaml"))
if "youtube" in pricing and "units_videos_insert" in pricing["youtube"]:
    print("  \033[32m✓ youtube quota units declared (verifiable against live API in Phase 6)\033[0m")
else:
    print("  \033[31m✗ youtube quota config missing\033[0m"); fail += 1
sys.exit(fail)
PY
if [[ $? -eq 0 ]]; then ok "config files valid"; else bad "config validation failed"; fi

# ---------------------------------------------------------------- 5. compose
step "5. docker-compose.yml is structurally valid"
python3 - <<'PY'
import sys, yaml
d = yaml.safe_load(open("docker-compose.yml"))
required = {"postgres", "redis", "api", "web", "beat",
            "worker-media", "worker-transcribe", "worker-analyze",
            "worker-render", "worker-qa", "worker-publish", "worker-analytics"}
missing = required - set(d["services"])
if missing:
    print(f"  \033[31m✗ missing services: {missing}\033[0m"); sys.exit(1)
for name in required:
    assert "image" in d["services"][name] or "build" in d["services"][name], name
print(f"  \033[32m✓ {len(d['services'])} services, all 7 queues + beat + infra present\033[0m")
PY
if [[ $? -eq 0 ]]; then ok "compose valid"; else bad "compose invalid"; fi

# ---------------------------------------------------------------- 6. env parity
step "6. Every settings field is documented in .env.example"
cd "${REPO_ROOT}/backend"
python3 - <<'PY'
import re, sys
sys.path.insert(0, ".")
from app.core.config import Settings
env_text = open("../.env.example").read().upper()
fields = [f for f in Settings.model_fields if not f.startswith("_")]
# Best-effort parity: each setting should appear (as ENV_NAME) in .env.example.
missing = [f for f in fields if f.upper() not in env_text]
if missing:
    print(f"  \033[33m! settings not in .env.example: {missing}\033[0m")
else:
    print(f"  \033[32m✓ all {len(fields)} settings documented in .env.example\033[0m")
sys.exit(1 if missing else 0)
PY
if [[ $? -eq 0 ]]; then ok ".env.example parity"; else bad ".env.example missing settings"; fi
cd "${REPO_ROOT}"

# ---------------------------------------------------------------- 7. unit tests
step "7. Backend unit tests"
cd "${REPO_ROOT}/backend"
if python3 -m pytest tests/unit >/tmp/phase0_pytest.log 2>&1; then
  ok "$(grep -Eo '[0-9]+ passed[^=]*' /tmp/phase0_pytest.log | tail -1)"
else
  bad "unit tests failed"; tail -25 /tmp/phase0_pytest.log
fi

# ---------------------------------------------------------------- 8. media fixtures
cd "${REPO_ROOT}"
step "8. Test media fixtures generate correctly (real FFmpeg)"
if ./ops/scripts/make_fixtures.sh >/tmp/phase0_fixtures.log 2>&1; then
  ok "$(grep -c '✓' /tmp/phase0_fixtures.log) fixtures verified, $(du -sh backend/tests/fixtures/generated 2>/dev/null | cut -f1) total"
else
  bad "fixture generation failed"; tail -15 /tmp/phase0_fixtures.log
fi

# ---------------------------------------------------------------- 9. no fake integrations
step "9. No mock/fake behaviour in production code paths"
HITS=$(grep -rn --include=*.py -E '(if +.*environment *=+ *"test"|FAKE_|DUMMY_|MOCK_|mock_response *=|return +\{?["'"'"']success["'"'"'])' app/ 2>/dev/null | grep -v '^app/.*#' || true)
if [[ -z "${HITS}" ]]; then
  ok "no test/date branches or fake-success returns in app/"
else
  bad "suspicious test/fake code in app/:"; echo "${HITS}"
fi

# ---------------------------------------------------------------- summary
printf '\n\033[1m════════════════════════════════════════════\033[0m\n'
printf '  Phase 0 verification: \033[32m%d passed\033[0m, ' "${PASS}"
if [[ ${FAIL} -gt 0 ]]; then printf '\033[31m%d failed\033[0m\n' "${FAIL}"; else printf '0 failed\n'; fi
printf '\033[1m════════════════════════════════════════════\033[0m\n'
[[ ${FAIL} -eq 0 ]] || exit 1

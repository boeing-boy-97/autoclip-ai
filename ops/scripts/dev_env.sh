#!/usr/bin/env bash
# =====================================================================================
# Create a local .env with real secrets.
#
# `cp .env.example .env` does NOT work: the settings validator rejects placeholder secrets
# (SECRET_KEY=REPLACE_ME_...) at boot, in every environment, on purpose. This script generates
# the three values that cannot be defaulted, keeps everything else from .env.example, and never
# overwrites an existing .env.
#
# Usage: ./ops/scripts/dev_env.sh
# =====================================================================================
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

if [[ -f .env ]]; then
  echo ".env already exists — leaving it untouched."
  exit 0
fi

if [[ ! -f .env.example ]]; then
  echo "error: .env.example is missing" >&2
  exit 1
fi

python3 - <<'PY'
import base64, os, secrets

key = lambda: base64.b64encode(os.urandom(32)).decode()
pepper = secrets.token_urlsafe(32)

replacements = {
    "SECRET_KEY": key(),
    "SECRET_ENCRYPTION_KEY": key(),
    "PASSWORD_PEPPER": pepper,
}

lines = []
with open(".env.example", encoding="utf-8") as fh:
    for line in fh:
        name = line.split("=", 1)[0].strip()
        if name in replacements and not line.lstrip().startswith("#"):
            lines.append(f"{name}={replacements[name]}\n")
        else:
            lines.append(line)

with open(".env", "w", encoding="utf-8") as fh:
    fh.writelines(lines)
print("wrote .env with generated secrets")
PY

echo "Next: docker compose up --build"

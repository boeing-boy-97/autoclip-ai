#!/usr/bin/env python3
"""Validate docs/openapi.json for structural correctness and contract hygiene.

Checks (all must pass):
  1. Valid OpenAPI 3.1 per openapi-spec-validator.
  2. Unique operationIds.
  3. Every operation declares security.
  4. Every {path} placeholder is declared as a path parameter.
  5. Every operation returns at least one error response using problem+json,
     except liveness endpoints whitelisted below.
  6. Every $ref resolves.
  7. Paginated schemas expose next_cursor.
  8. Error codes referenced in x-error-codes look like snake_case machine codes.

Run: python3 ops/scripts/validate_openapi.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import sys

from openapi_spec_validator import validate

SPEC_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "openapi.json"
METHODS = {"get", "post", "patch", "put", "delete"}

# Liveness/readiness probes legitimately return no problem+json body.
ERROR_SHAPE_EXEMPT = {"/api/v1/health/livez", "/api/v1/health/readyz"}

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text())
    validate(spec)
    print("✅ valid OpenAPI 3.1")

    ops: list[tuple[str, str, dict]] = [
        (path, m, o) for path, item in spec["paths"].items()
        for m, o in item.items() if m in METHODS
    ]

    dupes = [k for k, v in collections.Counter(o["operationId"] for _, _, o in ops).items() if v > 1]
    if dupes:
        fail(f"duplicate operationIds: {dupes}")

    for path, method, o in ops:
        if not o.get("security"):
            fail(f"no security: {method.upper()} {path}")
        declared = {p["name"] for p in o.get("parameters", []) if p.get("in") == "path"}
        for name in re.findall(r"\{([a-z_]+)\}", path):
            if name not in declared:
                fail(f"undeclared path param '{name}': {method.upper()} {path}")
        if path not in ERROR_SHAPE_EXEMPT:
            shapes = {
                ctype.split(";")[0].strip()
                for code, resp in o["responses"].items()
                if not code.startswith("2")
                for ctype in resp.get("content", {})
            }
            if "application/problem+json" not in shapes:
                fail(f"no problem+json error response: {method.upper()} {path}")

    raw = SPEC_PATH.read_text()
    for ref in set(re.findall(r'"\\$ref":\\s*"#/components/schemas/([A-Za-z0-9_]+)"', raw)):
        if ref not in spec["components"]["schemas"]:
            fail(f"unresolved $ref: {ref}")

    for name, sch in spec["components"]["schemas"].items():
        if name.endswith("Page"):
            props = sch.get("properties", {})
            if "next_cursor" not in props or "items" not in props:
                fail(f"paginated schema {name} missing items/next_cursor")

    for code in spec.get("x-error-codes", []):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", code):
            fail(f"error code not snake_case: {code}")

    required_paths = {
        "/api/v1/auth/register", "/api/v1/auth/login", "/api/v1/auth/logout", "/api/v1/auth/me",
        "/api/v1/projects", "/api/v1/projects/{project_id}", "/api/v1/sources/upload",
        "/api/v1/sources/youtube", "/api/v1/projects/{project_id}/analyze",
        "/api/v1/projects/{project_id}/analysis", "/api/v1/projects/{project_id}/transcript",
        "/api/v1/projects/{project_id}/moments", "/api/v1/projects/{project_id}/generate-clips",
        "/api/v1/projects/{project_id}/clips", "/api/v1/clips/{clip_id}",
        "/api/v1/clips/{clip_id}/regenerate", "/api/v1/clips/{clip_id}/approve",
        "/api/v1/clips/{clip_id}/reject", "/api/v1/clips/{clip_id}/versions",
        "/api/v1/clips/{clip_id}/render", "/api/v1/clips/{clip_id}/edit-settings",
        "/api/v1/clips/{clip_id}/quality-check", "/api/v1/clips/{clip_id}/quality-report",
        "/api/v1/youtube/channels", "/api/v1/youtube/oauth/start", "/api/v1/youtube/oauth/callback",
        "/api/v1/clips/{clip_id}/publish", "/api/v1/clips/{clip_id}/schedule",
        "/api/v1/publishing/history", "/api/v1/analytics/overview",
        "/api/v1/analytics/channels/{channel_id}", "/api/v1/analytics/clips/{clip_id}",
        "/api/v1/jobs/{job_id}", "/api/v1/jobs/{job_id}/retry", "/api/v1/jobs/{job_id}/cancel",
        "/api/v1/health",
    }
    missing = required_paths - set(spec["paths"])
    if missing:
        fail(f"spec endpoints missing from contract: {sorted(missing)}")

    if failures:
        print(f"\n❌ {len(failures)} contract problem(s):")
        for f in failures:
            print("   -", f)
        return 1

    print(f"✅ {len(ops)} operations across {len(spec['paths'])} paths, "
          f"{len(spec['components']['schemas'])} schemas — all contract checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

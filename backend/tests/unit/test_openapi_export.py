"""The committed implemented-API spec must match what the app actually serves."""

from __future__ import annotations

import json
import pathlib

from app.scripts.export_openapi import DEFAULT_OUT, build_spec, count_operations

# Routes added in Phase 2. Guarded here so a route that gets registered but never exported
# (or an export that silently drops a router) fails loudly.
PHASE2_PATHS = {
    "/api/v1/workspaces/{workspace_id}/projects/{project_id}/analyze",
    "/api/v1/workspaces/{workspace_id}/projects/{project_id}/analysis",
    "/api/v1/workspaces/{workspace_id}/projects/{project_id}/transcript",
    "/api/v1/workspaces/{workspace_id}/projects/{project_id}/moments",
}


def test_committed_spec_matches_app():
    committed = json.loads(DEFAULT_OUT.read_text())
    live = json.loads(json.dumps(build_spec()))
    assert committed == live, (
        "docs/openapi-implemented.json is out of date — run `python -m app.scripts.export_openapi`"
    )


def test_analysis_routes_are_exported():
    paths = build_spec()["paths"]
    missing = PHASE2_PATHS - set(paths)
    assert not missing, f"analysis routes missing from spec: {sorted(missing)}"


def test_every_path_is_prefixed():
    """A path outside /api/v1 would leak past any future gateway prefix rules."""
    for path in build_spec()["paths"]:
        assert path.startswith("/api/v1/"), path


def test_operations_are_counted_not_guessed():
    spec = build_spec()
    # 20 paths but more operations: /analyze and /analysis share a path, as do others.
    assert count_operations(spec) >= len(spec["paths"])


def test_out_path_lives_in_docs():
    assert pathlib.Path(DEFAULT_OUT).parent.name == "docs"
    assert pathlib.Path(DEFAULT_OUT).name == "openapi-implemented.json"

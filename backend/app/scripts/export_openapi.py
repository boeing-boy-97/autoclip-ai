"""Export the FastAPI app's live OpenAPI schema to a JSON file.

This is the export step promised in ``ops/scripts/gen_openapi.py``'s docstring: once the app
became the source of truth, the *implemented* API surface must be exportable rather than
hand-maintained.

It deliberately does **not** overwrite ``docs/openapi.json``. That file is the Phase 0
*product design* contract — it declares every endpoint the product intends to ship (62
operations), and ``ops/scripts/validate_openapi.py`` enforces a required-path list against it.
Clobbering it with today's 20 implemented paths would delete the roadmap and fail CI.

So the two artefacts stay separate and honest:

  docs/openapi.json             the designed contract (aspirational, validator-enforced)
  docs/openapi-implemented.json what the app actually serves right now (this script)

Diffing the two is how you see the remaining work.

Run:
    python -m app.scripts.export_openapi
    python -m app.scripts.export_openapi --out /tmp/spec.json --check
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

DEFAULT_OUT = pathlib.Path(__file__).resolve().parents[3] / "docs" / "openapi-implemented.json"


def build_spec() -> dict:
    """Import the app and return its OpenAPI document.

    Imported lazily so ``--help`` works without a configured environment.
    """
    from app.main import app  # noqa: PLC0415

    return app.openapi()


def count_operations(spec: dict) -> int:
    return sum(len([k for k in item if k in METHODS]) for item in spec.get("paths", {}).values())


def summarise(spec: dict) -> str:
    paths = spec.get("paths", {})
    schemas = spec.get("components", {}).get("schemas", {})
    return f"{count_operations(spec)} operations, {len(paths)} paths, {len(schemas)} schemas"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the committed file is out of date",
    )
    args = parser.parse_args(argv)

    rendered = json.dumps(build_spec(), indent=2, sort_keys=True) + "\n"

    if args.check:
        if not args.out.exists():
            print(f"missing {args.out} — run this script without --check", file=sys.stderr)
            return 1
        if args.out.read_text() != rendered:
            print(
                f"{args.out} is out of date — regenerate with "
                "`python -m app.scripts.export_openapi`",
                file=sys.stderr,
            )
            return 1
        print(f"up to date: {args.out} ({summarise(json.loads(rendered))})")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered)
    print(f"wrote {args.out} — {summarise(json.loads(rendered))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

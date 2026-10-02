#!/usr/bin/env python
"""Write the studio service's OpenAPI to docs/studio-openapi.json.

The Director's Desk generates its API types from this file and fails its build
when they drift, so the file has to be exactly what the service serves at
``/openapi.json`` — and has to change only when the API does. Hence:

- It is built from ``pipeline.webapp:app``, the object ``pipeline/Dockerfile``
  hands uvicorn, not from a second app assembled here. Paths are as the service
  serves them (``/api/…``): nginx strips ``/studio`` before proxying, and the
  prefixed alias mount is kept out of the schema.
- Offline: importing the app builds it, and building it creates the books
  folder, so ``AUDIOBOOK_OUTPUT`` points at a throwaway directory and the fake
  synth/director flags are on. No gateway, no model, no network, and nothing
  written to ``output/``.
- Deterministic: sorted keys, 2-space indent, UTF-8, LF, trailing newline.
- The schema's wording depends on the installed FastAPI and Pydantic, so the
  file records the versions that produced it (``x-generated-with``, the one key
  the service itself does not serve). A machine with other versions — the Mac,
  the pinned Docker image — cannot tell drift from version noise, so the
  freshness test skips there instead of failing; ``--check`` says the same.

Examples
--------
    python scripts/export_studio_openapi.py            # rewrite docs/studio-openapi.json
    python scripts/export_studio_openapi.py --check    # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The schema carries the handlers' docstrings (em dashes, arrows); a Windows
# console defaults to cp1252 and would die printing a stale-file diff of them.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # already UTF-8, or not a real tty
        pass

DEFAULT_OUT = ROOT / "docs" / "studio-openapi.json"
REGENERATE = "python scripts/export_studio_openapi.py"


GENERATED_WITH = "x-generated-with"


def versions() -> str:
    import fastapi
    import pydantic

    return f"fastapi {fastapi.__version__}, pydantic {pydantic.VERSION}"


def render() -> str:
    """The export as text. Imports the app, so the caller owns the environment
    (``main`` sets an offline one; the freshness test uses its fixture's)."""
    from pipeline.webapp import app

    schema = {**app.openapi(), GENERATED_WITH: versions()}
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def recorded_versions(path: Path) -> str | None:
    """The FastAPI/Pydantic versions that produced ``path``, if it says."""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(GENERATED_WITH)
    except (FileNotFoundError, ValueError):
        return None


def check(path: Path) -> str | None:
    """None when ``path`` matches a fresh export, else what to do about it."""
    fresh = render()
    try:
        committed = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return f"{path.name} does not exist. Create it: {REGENERATE}"
    if committed == fresh:
        return None
    diff = list(
        difflib.unified_diff(
            committed.splitlines(), fresh.splitlines(), "committed", "fresh", lineterm="", n=1
        )
    )
    shown = "\n".join(diff[:40]) + ("\n..." if len(diff) > 40 else "")
    recorded = recorded_versions(path)
    if recorded != versions():
        return (
            f"{path.name} differs from a fresh export, but it was made with "
            f"{recorded or 'unrecorded versions'} and this machine has {versions()}, so the "
            f"difference may be version noise rather than an API change. Compare on the "
            f"machine that exported it, or regenerate here only if the API changed: "
            f"{REGENERATE}\n\n{shown}"
        )
    return (
        f"{path.name} is stale: the studio's routes or wire models changed since it was "
        f"exported.\nRegenerate it and commit the result: {REGENERATE}\n"
        f"(fresh export built with {versions()})\n\n{shown}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is stale; write nothing")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="default: docs/studio-openapi.json")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="studio-openapi-") as scratch:
        os.environ["AUDIOBOOK_OUTPUT"] = scratch
        os.environ["STUDIO_OFFLINE"] = "1"
        os.environ["STUDIO_FAKE_DIRECTOR"] = "1"
        if args.check:
            problem = check(args.out)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            print(f"{args.out.name} is fresh ({versions()})")
            return 0
        text = render()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(text.encode("utf-8"))
    schema = json.loads(text)
    print(
        f"wrote {args.out} — {len(schema['paths'])} paths, "
        f"{len(schema['components']['schemas'])} schemas ({versions()})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

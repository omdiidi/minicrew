"""Asserts every file in tests/schema_negatives/ FAILS jsonschema validation.

If any of them pass (i.e. the schema is too permissive), exit 1 and name the file.
Exit 0 when every negative fails as expected.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "config.schema.json"
NEGATIVES_DIR = Path(__file__).resolve().parent / "schema_negatives"


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    files = sorted(NEGATIVES_DIR.glob("*.yaml"))
    if not files:
        print("FAIL: no negative fixtures found in", NEGATIVES_DIR, file=sys.stderr)
        return 1

    failures: list[str] = []
    for f in files:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(data, schema=schema)
        except jsonschema.ValidationError as e:
            print(f"OK {f.name} rejected: {e.message}")
            continue
        # Passed validation -> the schema is too loose.
        print(f"FAIL {f.name} unexpectedly passed validation", file=sys.stderr)
        failures.append(f.name)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Renders every example prompt template with a dummy payload.

Uses the same Jinja environment as worker/config/render.py (StrictUndefined +
JSON-safe finalize). Exits 1 on any UndefinedError or template error.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from worker.config.render import build_env  # noqa: E402

DUMMY_JOB = {
    "id": "dummy-job-id",
    "job_type": "dummy",
    "status": "running",
    "priority": 0,
    "attempt_count": 0,
}
DUMMY_PAYLOAD = {
    "text": "dummy text content for rendering.",
    "documents": [
        {"title": "doc-a", "body": "alpha body"},
        {"title": "doc-b", "body": "beta body"},
    ],
}
DUMMY_CONFIG_VIEW = {
    "schema_version": 1,
    "worker": {"prefix": "minicrew", "role": "primary", "poll_interval_seconds": None},
    "reaper": {"stale_threshold_seconds": 120, "interval_seconds": 60, "max_attempts": 3},
    "job_types": {"dummy": {"mode": "single", "model": "claude-sonnet-4-6"}},
}
DUMMY_GROUP = {"name": "first_third", "document_indices": [0, 1]}
DUMMY_GROUP_RESULT_PATHS = ["/tmp/g1.json", "/tmp/g2.json", "/tmp/g3.json"]


def _render_one(prompts_dir: Path, filename: str) -> tuple[bool, str]:
    env = build_env(prompts_dir)
    try:
        tmpl = env.get_template(filename)
        ctx = {
            "job": DUMMY_JOB,
            "payload": DUMMY_PAYLOAD,
            "config": DUMMY_CONFIG_VIEW,
            "group": DUMMY_GROUP,
            "group_result_paths": DUMMY_GROUP_RESULT_PATHS,
        }
        tmpl.render(**ctx)
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _iter_examples():
    examples = REPO_ROOT / "examples"
    for child in sorted(examples.iterdir()):
        if not child.is_dir():
            continue
        cfg_file = child / "config.yaml"
        if not cfg_file.exists():
            continue
        prompts_dir = child / "prompts"
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        # Collect every template filename referenced by the config.
        files: set[str] = set()
        for jt in (cfg.get("job_types") or {}).values():
            if jt.get("prompt_template"):
                files.add(jt["prompt_template"])
            for g in jt.get("groups") or []:
                if g.get("prompt_template"):
                    files.add(g["prompt_template"])
            merge = jt.get("merge") or {}
            if merge.get("prompt_template"):
                files.add(merge["prompt_template"])
        for f in sorted(files):
            yield child.name, prompts_dir, f


def main() -> int:
    failed = 0
    for example_name, prompts_dir, filename in _iter_examples():
        ok, err = _render_one(prompts_dir, filename)
        label = f"{example_name}/{filename}"
        if ok:
            print(f"OK {label}")
        else:
            print(f"FAIL {label}: {err}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

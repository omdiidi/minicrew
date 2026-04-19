"""Jinja rendering with StrictUndefined + JSON-safe finalize callback."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from worker.config.models import Config, JobType


def _finalize(value: Any) -> str:
    """Jinja finalize callback: strings pass through; dict/list/number are JSON-encoded.

    Plain-string interpolation (`{{ payload.title }}`) emits the string verbatim so existing
    quoted prose works. Any non-string (dict, list, int, bool) emits JSON — which keeps
    templates safe when payload values are structured rather than plain prose.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


def build_env(prompts_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        autoescape=False,
        undefined=StrictUndefined,
        finalize=_finalize,
    )


def render_prompt(cfg: Config, job_type: JobType, job: dict) -> str:
    env = build_env(cfg.prompts_dir)
    tmpl = env.get_template(job_type.prompt_template)
    rendered = tmpl.render(
        job=job,
        payload=job.get("payload") or {},
        config=cfg.public_view(),
    )
    if job_type.skill:
        rendered = f"/{job_type.skill}\n\n{rendered}"
    return rendered


def render_named_template(cfg: Config, template_filename: str, **ctx: Any) -> str:
    """Render an ad-hoc template file (used by fan_out group + merge prompts)."""
    env = build_env(cfg.prompts_dir)
    tmpl = env.get_template(template_filename)
    return tmpl.render(**ctx)

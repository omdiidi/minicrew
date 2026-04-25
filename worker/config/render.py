"""Jinja rendering with StrictUndefined + JSON-safe finalize callback."""
from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from jinja2 import DictLoader, Environment, FileSystemLoader, StrictUndefined

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


def _builtin_env(template_name: str) -> Environment:
    """Build a Jinja env around a single packaged template under worker.builtin_prompts."""
    src = files("worker.builtin_prompts").joinpath(template_name).read_text(encoding="utf-8")
    return Environment(
        loader=DictLoader({template_name: src}),
        autoescape=False,
        undefined=StrictUndefined,
        finalize=_finalize,
    )


def render_builtin_ad_hoc(
    *,
    cfg: Config,
    job: dict,
    payload: dict,
    task: str,
    allow_code_push: bool,
    repo_path: str,
    repo_url: str,
    sha: str,
    result_filename: str,
) -> str:
    """Render the built-in ad_hoc wrapper template (worker/builtin_prompts/ad_hoc.md.j2)."""
    env = _builtin_env("ad_hoc.md.j2")
    return env.get_template("ad_hoc.md.j2").render(
        job=job,
        payload=payload,
        config=cfg.public_view(),
        task=task,
        allow_code_push=allow_code_push,
        repo={"path": repo_path, "url": repo_url, "sha": sha},
        result_filename=result_filename,
    )


def render_builtin_handoff(
    *,
    cfg: Config,
    job: dict,
    payload: dict,
    user_instruction: str | None,
    allow_code_push: bool,
    result_filename: str,
    job_id: str,
) -> str:
    """Render the built-in handoff continuation template (worker/builtin_prompts/handoff.md.j2)."""
    env = _builtin_env("handoff.md.j2")
    return env.get_template("handoff.md.j2").render(
        job=job,
        payload=payload,
        config=cfg.public_view(),
        user_instruction=user_instruction,
        allow_code_push=allow_code_push,
        result_filename=result_filename,
        job_id=job_id,
    )

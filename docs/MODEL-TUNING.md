# Model Tuning

## For LLMs

Guidance on picking a Claude model and thinking budget per job type. The three defaults here —
`claude-sonnet-4-6` and `thinking_budget: medium` — are the calibrated baseline; do not lower
them without measurement. Per-job-type overrides live in `config.yaml` under
`job_types.<name>.model` and `job_types.<name>.thinking_budget`. Do not invent model names; the
canonical list is the three below. Keep the defaults table current when new models ship.

## Why this matters

Cost, latency, and quality are three different dimensions and they push against one another
asymmetrically. Shipping every job with the default model and default thinking budget is a
safe starting point, but it often leaves you paying twice what you need to for classification
tasks and running half the quality you could for reasoning tasks. The right knob setting per
job type is usually obvious after you run a few samples and compare.

## Models

Three models are supported in minicrew's configuration:

- `claude-opus-4-7` is the highest-quality and most expensive. Pick it when you cannot afford
  a wrong answer, when the task involves multi-step reasoning, or when the output is going to
  be acted on by a downstream system without human review.
- `claude-sonnet-4-6` is the balanced default. It handles summarization, analysis,
  classification with reasoning, and most common job shapes well. When in doubt, start here.
- `claude-haiku-4-5` is the fastest and cheapest. Pick it for rote extraction, classification
  on short inputs, straightforward formatting, and any task where the correct answer is nearly
  deterministic given the input.

## Thinking budget

Thinking budget controls how much reasoning the model does before producing its final answer.
Three values are accepted: `none`, `medium`, and `high`. `medium` is the default and suits
almost every job. `high` dramatically increases output quality on reasoning-heavy tasks (math,
multi-document synthesis, structured planning) at the cost of latency and tokens. `none` is
appropriate only for rote tasks where no chain-of-thought is meaningful — short classifications,
one-shot extractions, trivial formatting.

## How to measure

Pick ten representative jobs of a given job type. Run them through the current config and save
the `result` and `completed_at - started_at` for each. Then run the same ten through a
candidate config (different model, different budget) and compare. You are looking for three
signals: quality (is the answer still correct, subjectively or against a rubric), latency (is
the p50 duration within your product's tolerance), and cost (roughly proportional to model tier
and thinking budget). Move to the cheaper configuration whenever quality holds. Move to the
more expensive configuration whenever quality is visibly lacking.

## Changing per job type

The conversational path is `/minicrew:tune <job_type>`. The skill walks you through the model
and budget choice, edits `worker-config/config.yaml` in place, and reports what changed. You
can also edit the YAML directly; the worker picks up the change on the next job claim after a
restart. There is no hot reload in v1.

## Recommended defaults by task class

| Task class | Model | Thinking budget | Notes |
| --- | --- | --- | --- |
| Classification, short input | `claude-haiku-4-5` | `none` | Fastest, cheapest; high-volume. |
| Extraction from structured input | `claude-haiku-4-5` | `medium` | Medium budget prevents silly mistakes. |
| Summarization | `claude-sonnet-4-6` | `medium` | The default case. |
| Analysis across one document | `claude-sonnet-4-6` | `medium` | The default case. |
| Multi-document synthesis | `claude-sonnet-4-6` | `high` | Budget pays for cross-document reasoning. |
| Planning, multi-step reasoning | `claude-opus-4-7` | `high` | Where quality matters most. |
| Safety- or compliance-critical | `claude-opus-4-7` | `high` | Do not skimp. |

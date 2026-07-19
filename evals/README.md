# Local agent evaluations

This directory contains versioned, outcome-based evaluation cases and sanitized
baseline summaries. The first milestone evaluates `MainRouterAgent` with the
production prompt, tool schemas, AgentScope host, and a real DashScope model. It
does **not** require Redis, FastAPI, the frontend, or navigation processing
scripts.

## Environment

Export a DashScope credential and the same router model used by the deployed
service:

```bash
export DASHSCOPE_API_KEY="..."
export VLA_AGENT_MODEL="..."
# Optional production override:
export VLA_AGENT_ROUTER_MODEL="..."
```

`DASHSCOPE_BASE_URL` is honored when configured. Credentials are inherited by
the isolated worker process; they are never placed in command-line arguments,
request JSON, traces, or baseline reports.

## Commands

Validate case schemas and grader configuration without calling a model:

```bash
vla-agent-eval validate --suite router-smoke
```

Run all cases once, or select/repeat a case:

```bash
vla-agent-eval run --suite router-smoke
vla-agent-eval run --suite router-smoke --case router_shortcut_preserves_scope --repeat 3
```

Raw, sanitized artifacts are written to
`.artifacts/evaluation/<run-id>/` by default. Override this for a disposable run
with `--output-dir PATH`. To refresh the compact, commit-safe JSON and Markdown
summaries under `evals/baselines/`, add `--write-baseline`:

```bash
vla-agent-eval run --suite router-smoke --write-baseline
```

Exit codes are `0` when all attempts pass, `1` when any attempt is `FAIL` or
`TIMEOUT`, and `2` for invalid configuration, provider failures, or evaluation
infrastructure errors.

## Baseline policy

Evaluation cases score observable outcomes and safety invariants, not a fixed
tool-call script. Full model responses and event traces remain in the ignored
artifact directory; committed baselines contain only sanitized metadata,
metrics, statuses, hashes, and failure reasons. Thinking events, credentials,
and absolute workspace paths must never be persisted.

A model `FAIL` is a valid baseline result. Do not change an agent prompt, tool,
router, or business behavior while implementing or refreshing the evaluation
baseline. Record the failure first, then address it in a separate optimization
change and compare against the same cases.

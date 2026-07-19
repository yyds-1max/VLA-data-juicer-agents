# Local agent evaluations

This directory contains versioned, outcome-based evaluation cases and sanitized
baseline summaries. The first milestone evaluates `MainRouterAgent` with the
production prompt, tool schemas, AgentScope host, and a real DashScope model. It
does **not** require Redis, FastAPI, the frontend, or navigation processing
scripts.

The case Schema v1 intentionally supports exactly one `router` user turn.
`navigation`, `end_to_end`, and multi-turn conversations require separate
execution semantics, side-effect simulators, and graders; they are rejected at
validation time instead of being silently approximated by this runner.

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

Repeated runs include a per-case stability summary in stdout and
`aggregate.json`: `SINGLE_SAMPLE`, `STABLE_PASS`, `STABLE_FAIL`, `FLAKY`,
`TIMEOUT`, or `ERROR`. The summary also records pass rate, failure-signature
counts, and min/median/max latency and token metrics. These labels are
descriptive; three attempts do not imply statistical significance.

Raw, sanitized artifacts are written to
`.artifacts/evaluation/<run-id>/` by default. Override this for a disposable run
with `--output-dir PATH`. To refresh the compact, commit-safe JSON and Markdown
summaries under `evals/baselines/`, add `--write-baseline`:

```bash
vla-agent-eval run --suite router-smoke --write-baseline
```

`--write-baseline` requires the complete suite and refuses to replace the
baseline when a run contains an infrastructure `ERROR`. A model `FAIL` remains
a valid baseline result. Prefer the audited promotion workflow below for
repeated real-model runs.

Compare an existing baseline with a candidate aggregate:

```bash
vla-agent-eval compare \
  --baseline evals/baselines/router-smoke.json \
  --candidate .artifacts/evaluation/<run-id>/aggregate.json
```

Comparison requires the same suite, cases, model configuration, and AgentScope
version. Git, prompt, and tool-schema hashes may differ and are reported as the
changed variables. The command writes `comparison.json` and `comparison.md`
beside the candidate by default. Its exit code is `0` for no regression, `1`
for a behavioral regression or candidate timeout, and `2` for incompatible
reports, invalid input, or candidate infrastructure errors.

Comparison also requires the same evaluation contract version. Contract v2
grades the final public answer produced by the same event projection used by
the Web turn stream, rather than concatenating every model text delta. It also
redacts secrets and absolute paths again after streamed tool/text fields have
been reassembled. Historical reports without a contract anchor are treated as
contract v1 and are intentionally incompatible with contract v2 runs.

After reviewing a complete run, promote it without another model call:

```bash
vla-agent-eval promote \
  --input .artifacts/evaluation/<run-id>/aggregate.json \
  --suite router-smoke
```

Promotion requires a clean worktree, the current HEAD commit and case hash, a
complete suite with consecutive attempts, and no `ERROR` results. It writes the
JSON and Markdown baseline through temporary files. The recommended workflow is
to commit evaluation code first, run and compare on that clean commit, then
commit only the promoted baseline update.

Exit codes are `0` when all attempts pass, `1` when any attempt is `FAIL` or
`TIMEOUT`, and `2` for invalid configuration, provider failures, or evaluation
infrastructure errors.

## Baseline policy

Evaluation cases score observable outcomes and safety invariants, not a fixed
tool-call script. Full model responses and event traces remain in the ignored
artifact directory; committed baselines contain only sanitized metadata,
metrics, statuses, hashes, and failure signatures.
Report Schema v2 adds case stability summaries. The parser still reads v1
reports, while comparison requires matching evaluation contract versions.
Thinking events, credentials, absolute workspace paths, complete responses, and
tool payloads must never enter a baseline or comparison report.

A model `FAIL` is a valid baseline result. Do not change an agent prompt, tool,
router, or business behavior while implementing or refreshing the evaluation
baseline. Record the failure first, then address it in a separate optimization
change and compare against the same cases.

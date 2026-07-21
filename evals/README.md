# Local agent evaluations

This directory contains versioned, outcome-based evaluation cases and sanitized
baseline summaries. The default `datapilot-v1` suite evaluates the production
DataPilot Router V1 prompt, RouterContextEnvelope middleware, and exact
`start_navigation_data_task`, `continue_navigation_data_task`, and
`control_navigation_data_task` schemas. The host records accepted routing
operations but never starts navigation processing, so it does **not** require
Redis, FastAPI, the frontend, or navigation processing scripts.

Case Schema v2 supports one or more Router user turns and one optional safe
runtime setup: either a focused-task snapshot or a trusted shortcut
`request_context`. Assistant turns are always produced by the host; fixtures
cannot inject them. The trusted context is exposed only through the production
RouterContextEnvelope shape and its date/clip scope is enforced exactly by the
evaluation runtime. Navigation and end-to-end execution still require a
separate specialist simulator and are rejected rather than silently
approximated. Schema v1 and the `router-smoke` cases/baseline remain untouched as
historical artifacts. They are not the CLI default and are not a DataPilot V1
release gate.

The V1 suite covers capability answers, missing-date clarification, date-only
all-clips routing, single and multiple selected clips, clip/date prefix
independence, trusted shortcut scope, a two-turn date clarification that must
retain the selected clip, status queries, unrelated questions during an active
task, waiting-user continuation and normal close after rejecting
post-processing, paused-task resume, stop/cancel, occupied task-slot conflicts,
and the navigation generic-tool prohibition. Exact scope, allowed tool sets,
tool-call counts, and implementation-detail leakage are deterministic gates.

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
vla-agent-eval validate
# Equivalent: vla-agent-eval validate --suite datapilot-v1
```

Run all cases once, or select/repeat a case:

```bash
vla-agent-eval run
vla-agent-eval run --case router_start_selected_cross_date_prefix --repeat 3
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
vla-agent-eval run --suite datapilot-v1 --write-baseline
```

`--write-baseline` requires the complete suite and refuses to replace the
baseline when a run contains an infrastructure `ERROR`. A model `FAIL` remains
a valid baseline result. Prefer the audited promotion workflow below for
repeated real-model runs.

Compare an existing baseline with a candidate aggregate:

```bash
vla-agent-eval compare \
  --baseline evals/baselines/datapilot-v1.json \
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
  --suite datapilot-v1
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

No `datapilot-v1` baseline is committed until the suite is run with the real
deployed Router model and the resulting artifacts pass the existing safety
audit. Deterministic scripted host tests cover the V1 schemas, scope preservation,
single-call terminal handover, multi-turn context reuse, generic-tool blocking,
and trace redaction without pretending to be a real-model quality baseline.

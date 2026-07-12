# Navigation model-authored plan guidance

Navigation state has three authorities: investigation code appends factual observations,
the model authors one complete phase JSON plan, and the execution runtime advances the
ledger for the immutable stored plan. Conversation text is never authoritative state.

Every entry inspects raw, intermediate, and final artifacts before phase selection. Use
only the tools resolved for that durable phase. Complete every required observation;
when a detailed payload is needed, list and read its task-scoped evidence reference.
Unavailable resources are facts, not missing observations.

The model alone chooses sensor bindings, the time-sync reference and tolerance,
localization source/conversion, calibration source/mode, gridmap source, ordered steps,
variants, and business parameters. Code validates these choices against observed facts
and capability contracts but does not synthesize them.

Submit exactly one complete JSON plan through the active phase submission tool. Do not
send partial updates. On validation failure, use the bounded errors and evidence tools,
then resubmit a complete replacement. A successful response includes `ok: true` and the
immutable plan identity. Execution tools accept only `plan_id` and `step_id`; server code
loads canonical arguments from the plan repository.

Routine results and detailed evidence reads are bounded. Avoid repeating prior tool
payloads or plan candidates in assistant messages. After compaction or resume, recover
phase, active plan, and current step from the durable state tools.

Older deployments may still contain a `navigation-plan-drafts/` directory. Current code
does not read or write it. Operations may remove that directory only after rollout
verification; application code intentionally does not delete deployed state.

## Server acceptance runbook

Server acceptance is a post-deployment operation and is not completed by the local
test suite. Before running it, synchronize the server checkout to the exact reviewed
revision and record both local and server `git rev-parse HEAD`; stop if they differ.

Start with read-only checks for one known test date: service logs, the reconciled task
and artifact snapshot, observation revisions/evidence descriptors, active plan and
submission attempts, execution ledger/outbox rows, and the tool names resolved for the
durable phase. Record per-turn model input tokens, every tool-result character count,
compact events, plan revision, current step, and ledger transitions.

After the read-only state matches the synchronized revision, run the normal task in
dry-run mode. Confirm artifact reconciliation precedes planning, one valid complete
plan activates without any draft/finalize loop, peak input remains below 83,885 tokens,
and no standard-run compact event occurs. A forced-compaction check must recover phase,
active plan, and current step from SQLite rather than conversation text.

Only after dry-run evidence is reviewed may an operator run the separately authorized
real-data test. Deleting test outputs for a subsequent ordinary entry should select the
earliest incomplete phase. Do not change AgentScope compression or add phase sub-session
rotation during this acceptance; if the synchronized real run still exceeds the target,
preserve the transcript metrics and open a separate design task.

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

# Navigation Plan Server Acceptance

This is an operator-only runbook. Local implementation must not run a real server processing job. Deployment and real-data work require separate user approval.

## Approval and execution-mode boundary

Do not connect to, synchronize, restart, or mutate a server while completing local Tasks 1–8. After separate user approval, verify that the service starts with the production default `dry_run=False`. Use `dry_run=True` only for an explicitly selected test run through trusted runtime or direct-CLI configuration; never add it to router or model-facing tool arguments.

Stop before GUI/human steps unless the user is present and has explicitly asked to continue. Do not infer approval from an earlier dry-run, an accepted Plan, or a stored human-decision request.

## Deployment synchronization and Git checks

After separate approval, stop processing workers, synchronize only the approved code, and compare local and server `git rev-parse HEAD`. Confirm the configured state database, dataset roots, evidence root, processing root, and execution mode before starting the service. Stop if the revisions, expected diff, or configuration differ. Back up the navigation database and configured run/evidence roots before changing deployed state.

## Legacy storage cleanup

The redesigned service does not migrate incompatible navigation SQLite state or use `navigation-plan-drafts/`. With workers stopped, retain a backup, point the deployment at a fresh supported navigation-state database, and remove obsolete draft storage only after rollback evidence and the new deployment have been verified.

## Server log queries

For one approved test target, capture bounded logs for service startup, structured handoff JSON, task-attempt creation, model-invoked inspections, Plan submission, ledger transitions, outbox recovery, human-decision delivery, and errors. Verify a new Web session creates a fresh attempt even when completed history exists, and that only a genuinely running data writer can return `navigation_data_busy`. Do not collect unrelated service or dataset logs.

## Token measurement

Record every per-turn model input token count, every tool-result character count, exposed tool-schema size for each activity, compact events, Plan revision, and current ledger step. Assert `max_tool_result_chars <= 5_500`, `validation_failure_chars <= 3_000`, `peak_model_input_tokens <= 83_885`, and `compact_event_count == 0`. Preserve AgentScope compression and `ContextConfig(tool_result_limit=6000)`.

## Dry-run acceptance

Using trusted runtime configuration, run only the specifically approved dry-run target. Create a new Web session for `20270623 / 20260623_145550` and verify a fresh attempt is created despite historical attempts. Verify the NavigationDataAgent calls artifact inspection before choosing a stage, does not trust user claims, chooses a submission tool and complete Plan from current facts, executes only accepted canonical arguments, and truthfully reports the structured handoff/execution results. Collect handoff JSON plus bounded task, observation, Plan, ledger, tool-result, token, compact-event, and log evidence. After verified extract/sync, it must report and ask whether to continue rather than relying on a code-forced transition; apply the attended GUI/human boundary above.

## Real-data acceptance

Only after the synchronized revision, backups, read-only evidence, logs, token measurements, and dry-run are reviewed may an operator request separate approval for a real-data job. Reconfirm `dry_run=False` immediately before that separately approved job. Capture produced artifacts and validation evidence, avoid destructive retries without confirmation, and stop on revision mismatch, unexpected context growth, unauthorized tool exposure, contradictory product facts, or an unattended GUI/human boundary. Do not delete or alter unrelated server files, code, logs, or datasets.

Only if the optimized real-data transcript still reaches the compact trigger should phase-boundary AgentScope sub-session rotation be considered as a separate design change.

# Navigation Plan Server Acceptance

This is an operator-only runbook. Local implementation must not run a real server processing job. Deployment and real-data work require separate user approval.

## Deployment synchronization and Git checks

Stop processing workers, synchronize the server checkout to the reviewed commit, and compare local and server `git rev-parse HEAD`. Stop if the revisions or expected diff differ. Back up the navigation database and configured run/evidence roots before changing deployed state.

## Legacy storage cleanup

The redesigned service does not migrate incompatible navigation SQLite state or use `navigation-plan-drafts/`. With workers stopped, retain a backup, point the deployment at a fresh supported navigation-state database, and remove obsolete draft storage only after rollback evidence and the new deployment have been verified.

## Server log queries

For one approved test target, capture service startup, handoff, task-attempt creation, model-invoked inspections, Plan submission, ledger transitions, outbox recovery, human-decision delivery, and errors. Verify a new Web session creates a fresh attempt and that only a genuinely running data writer can return `navigation_data_busy`.

## Token measurement

Record per-turn model input tokens, tool-result character counts, prompt/tool-schema size, compact events, Plan revision, and current ledger step. Peak representative input must be at most 83,885 tokens; preserve AgentScope compression and `ContextConfig(tool_result_limit=6000)`.

## Dry-run acceptance

Using trusted runtime configuration, run an approved dry-run. Verify the NavigationDataAgent calls product inspection itself, does not trust user claims, chooses a submission tool and complete Plan from current facts, executes only accepted canonical arguments, and truthfully reports the structured handoff/execution results. After verified extract/sync, it must report and ask whether to continue rather than relying on a code-forced transition.

## Real-data acceptance

Only after the synchronized revision, backups, read-only evidence, logs, token measurements, and dry-run are reviewed may an operator request separate approval for a real-data job. Capture produced artifacts and validation evidence, avoid destructive retries without confirmation, and stop on revision mismatch, unexpected context growth, unauthorized tool exposure, or contradictory product facts.

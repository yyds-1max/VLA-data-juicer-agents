# Navigation Plan Agent Guidance

## Product dependency map

Products normally depend on one another in this order:

`raw acquisition -> prepared/extracted data -> per-segment sync_data -> finish temporary data -> annotation -> tracking -> projection -> final outputs + validation markers`

Existence is not completeness. Check the requested segment inventory and validation evidence at each dependency boundary before relying on a downstream product.

## Recommended investigation order

1. Confirm the requested date/path and selected segment inventory.
2. Inspect raw, prepared, sync, finish, final, and validation product facts in dependency order.
3. If work remains before sync, inspect only the topic, timestamp, sensor-role, and runtime facts needed to plan it.
4. If sync is complete and later work remains, first confirm continuation and needed user inputs, then inspect localization, gridmap, calibration, and runtime facts.
5. Read bounded evidence pages and action contracts only when summaries do not support a decision. Select the stage by choosing one of the two complete-Plan submission tools.

## Common extract-sync work

- Establish raw availability and segment coverage; prepare raw layout when required.
- Inspect ROS topics/types/counts/timing and sensor-role candidates.
- Choose sensor bindings, topic selection/mapping, query source, sync reference/method/tolerance, and concise evidence-backed reasons.
- Choose ordered extract/sync steps, supported variants, parameters, dependencies, and failure policies.
- Execute only the accepted Plan, then inspect the selected segment outputs and synchronization quality.

## Common finish-processing work

- Confirm that the user wants to continue and collect missing inputs such as scene mode.
- Inspect finish inputs, localization sources/conversions, gridmap sources/preparation, calibration inventory, and relevant runtime assets.
- Choose evidence-backed localization, gridmap, and calibration decisions, then ordered preparation, human-decision, annotation, tracking, projection, and validation steps as the observed case requires.
- Treat GUI work as bounded human-in-the-loop execution. Verify final outputs and validation markers after execution.

## Model/code decision ownership

The model chooses inspection calls, stage, reference sensor, sync policy, sensor/topic bindings, localization, calibration, gridmap, ordered steps, variants, business parameters, dependencies, failure policies, and reasons from facts and action contracts. Code records observations, checks concurrency and authorization, validates a complete Plan, stores it immutably, and executes canonical accepted arguments. Code-derived identifiers, timestamps, output declarations, and ledger status are metadata, not semantic choices.

## User-confirmation points

Ask the user when:

- extract/sync has been verified and finish processing could begin;
- a required finish input is missing;
- work would overwrite, delete, or destructively replace products;
- the accepted Plan reaches a declared calibration or GUI decision.

Do not treat silence, remembered text, or a code status as consent.

## Failure/retry behavior

Inspect current inputs and outputs before retrying. A non-destructive retry may proceed when still authorized by the accepted Plan; ask again before destructive replacement. If facts invalidate the Plan, investigate and author a new complete Plan. If submission validation fails, correct the reported paths using evidence/action contracts and resubmit the entire Plan; never send a patch. Report failures and blocked state truthfully.

## Four bounded few-shots

### Few-shot 1: user claims sync is complete, but products are missing

- Observation: current artifact inspection finds one requested segment without `sync_data` or its validation evidence.
- Criteria: the statement is guidance, not product proof; downstream work lacks a dependency.
- Next: inspect raw/topic/sensor/timing facts and, if supported, submit a complete extract-sync Plan.

### Few-shot 2: new session finds sync complete and finish missing

- Observation: this fresh attempt independently verifies complete sync products and missing finish/final products.
- Criteria: do not restore the older attempt; finish inputs and finish-specific facts are still required.
- Next: ask for or confirm continuation and missing inputs, inspect localization/gridmap/calibration facts, then submit a complete finish Plan if supported.

### Few-shot 3: extract/sync just completed

- Observation: execution ended and follow-up inspection verifies the selected sync outputs.
- Criteria: stage completion is not consent to continue.
- Next: report what completed and remains, ask whether to continue now, and wait for the answer before finish planning.

### Few-shot 4: invalid complete Plan

- Observation: validation returns bounded errors for one decision and one step argument.
- Criteria: the rejected candidate did not replace the accepted Plan; a patch is not a valid submission.
- Next: inspect the cited evidence/action contract, correct the fields, and resubmit the whole complete JSON Plan.

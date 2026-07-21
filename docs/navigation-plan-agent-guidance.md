# Navigation Plan Agent Guidance

## Product dependency map

Products normally depend on one another in this order:

`raw acquisition -> prepared/extracted data -> per-clip sync_data (containing internally generated segments/sequences) -> finish temporary data -> annotation -> tracking -> projection -> final outputs + validation markers`

The task-selection granularity is clips. A segment or sequence is generated inside a clip by synchronization and is never a selectable task input or a substitute clip identifier. Do not narrow, split, or redirect a task based on an internal segment/sequence name.

Existence is not completeness. Check the requested clip inventory and validation evidence at each dependency boundary before relying on a downstream product.

## Recommended investigation order

1. Confirm the requested date/path and selected clip inventory. Treat internal segment/sequence names only as product evidence inside those clips.
2. Inspect raw, prepared, sync, finish, final, and validation product facts in dependency order.
3. If work remains before sync, inspect only the topic, timestamp, sensor-role, and runtime facts needed to plan it.
4. If sync is complete and later work remains, first confirm continuation and needed user inputs, then inspect localization, gridmap, calibration, and runtime facts.
5. Read bounded evidence pages and action contracts only when summaries do not support a decision. Select the stage by choosing one of the two complete-Plan submission tools.

## Common extract-sync work

- Establish raw availability and clip coverage; prepare raw layout when required.
- Inspect ROS topics/types/counts/timing and sensor-role candidates. Identify the actual front-fisheye camera topic, lidar topic, and localization source from the selected clips; do not infer them from the date or platform name alone.
- Front-fisheye recordings commonly use `/cam_video4/csi_cam/image_raw/compressed` or `/cam_video5/csi_cam/image_raw/compressed`. Lidar commonly uses `/lidar_points`, `/rs32_lidar_points`, or `/r32_rslidar_points`. Treat these as known aliases, not an exhaustive list; use message types and evidence when another observed name is plausible.
- Prefer a native Ins topic such as `/drivers/ins/Ins` for localization when present and supported. Otherwise select the exact observed odom topic, commonly `/utlidar/robot_odom_systime` or `/sport_odom`; later finish processing normally requires `odom_to_ins` conversion, while native Ins uses no conversion.
- Select only topics justified by the chosen bindings. Do not add every observed topic to the extraction whitelist merely because it exists.
- Fill `topic_whitelist` with full observed ROS topic names. Fill `topic_map` with `extracted_dir -> output_dir` entries from topic-route evidence; it is not a ROS-topic-to-role map. Typical routes include `cam_video4|cam_video5 -> fisheye_front`, `lidar_points|rs32_lidar_points|r32_rslidar_points -> r32_rslidar_points`, `sport_odom|utlidar -> odom`, and `Ins -> Ins`.
- Fill `query_dir` with exactly one relative extracted directory under `tmp_dir`, never a ROS topic or filesystem path. It must be the extracted directory of `time_sync.reference_sensor`. Lidar is normally the reference because of its low frame rate; an observed extracted gridmap stream may be selected when the data actually contains one.
- Synchronization uses the company-standard nearest-timestamp tolerance of 100 ms. This is a fixed system policy, not a model-authored Plan parameter.
- Choose ordered extract/sync steps, supported variants, parameters, dependencies, and failure policies.
- Execute only the accepted Plan, then inspect the selected clip outputs, including their internally generated segment/sequence products, and synchronization quality.

## Common finish-processing work

- Confirm that the user wants to continue and explicitly ask whether the selected data is indoor or outdoor. Record it through `record_navigation_user_guidance_tool` as task `scene_mode` (`in` or `out`); it is currently informational and must not change execution branches yet.
- Inspect finish inputs, localization sources/conversions, gridmap sources/preparation, calibration inventory, and relevant runtime assets. A native Ins source normally skips odom conversion. An odom source normally requires the supported odom-to-Ins conversion before consumers that expect Ins-formatted data.
- Keep the downstream localization pipeline consistent: native Ins uses `main_smart.py`, `4_speed_direction_Ins.py`, and `cjl_with_gridmap`; odom uses conversion/resize, `main_smart_odom.py`, `4_speed_direction_odom.py`, and `cjl_0525_with_gridmap`.
- If an extracted gridmap already exists, inspect and reuse it when valid. If the platform has no recorded gridmap but synchronized lidar point clouds exist, select generation from PCD. Do not claim PCD/gridmap availability before extract/sync outputs contain the required files.
- Select calibration from the camera/platform calibration inventory and current evidence. For the current deployment, recommend `NoobScenes/params/20260529_go2w/sensors` by default when that exact source is present in the observed inventory. Present it as the current business default, set calibration `mode` to `selected_profile`, keep `requires_user_confirmation` true, and ask the user to confirm it before execution.
- Never choose a calibration profile merely because its directory name is newest, is lexicographically last, or resembles the data date. If the current default is absent, do not silently substitute another profile: list the observed candidates and ask the user which one to use. Every selected calibration profile still requires explicit user confirmation before copying.
- In an accepted Plan, execute the `confirm_navigation_calibration_params` action through its matching `confirm_navigation_calibration_params_tool`, passing only the current `plan_id` and `step_id`. The tool performs the external human-decision handoff; do not invent or call a differently named confirmation tool.
- Choose evidence-backed localization, gridmap, and calibration decisions, then ordered preparation, human-decision, annotation, tracking, projection, and validation steps as the observed case requires.
- Unless artifact inspection already proves both final outputs and non-empty final gridmaps are complete, include the full finish chain in business order: calibration confirmation, finish assembly, NoobScenes preprocessing, initial annotation, tracking, gridmap preparation, projection/trajectory, and final validation. Do not skip unseen work merely because an action is optional in the schema.
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

Treat `planning_context_revision` from `get_navigation_task_context_tool` as a one-time optimistic-concurrency token for the task context observed at that moment. Any later investigation or user-guidance update makes that revision stale. Continue investigating whenever necessary; after all investigation is complete, call `get_navigation_task_context_tool` again immediately before Plan submission and use its latest revision so the submitted Plan is based on the newest task context.

When a processing tool reports that it is running in the background, end the current reply immediately. Do not call `get_current_plan_step_tool`, `get_plan_execution_overview_tool`, or any other tool to poll or wait. The system will deliver the completion result and wake the same session automatically; read the current step again only after that completion notification or when recovering an idle session.

Inspect current inputs and outputs before retrying. A non-destructive retry may proceed when still authorized by the accepted Plan; ask again before destructive replacement. If facts invalidate the Plan, investigate and author a new complete Plan. If submission validation fails, correct the reported paths using evidence/action contracts and resubmit the entire Plan; never send a patch. Report failures and blocked state truthfully.

## Four bounded few-shots

### Few-shot 1: user claims sync is complete, but products are missing

- Observation: current artifact inspection finds one requested clip without complete `sync_data` or its validation evidence.
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

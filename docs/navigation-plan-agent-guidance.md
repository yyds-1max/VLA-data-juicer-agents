# Navigation Plan Agent Guidance

## Product dependency map

Products normally depend on one another in this order:

`raw acquisition -> prepared/extracted data -> per-clip sync_data (containing internally generated segments/sequences) -> Web initial annotation -> tracking -> postprocessing/projection -> immutable trajectory revision -> trajectory review/Fix -> approved training-compatible publication`

The task-selection granularity is clips. A segment or sequence is generated inside a clip by synchronization and is never a selectable task input or a substitute clip identifier. Do not narrow, split, or redirect a task based on an internal segment/sequence name.

The dataset date names the storage directory. Treat each requested clip ID as an opaque child-directory name: it may contain a different date-like prefix when a dataset was copied or renamed while metadata-backed clip names were preserved. Never rewrite, reject, or redirect a requested clip from that prefix. Verify the exact clip under the requested dataset date; only report it missing when current inventory inspection cannot find that exact pair.

Existence is not completeness. Check the requested clip inventory and validation evidence at each dependency boundary before relying on a downstream product.

## Recommended investigation order

1. Confirm the requested dataset directory and exact selected clip inventory without inferring a date from clip names. Treat internal segment/sequence names only as product evidence inside those clips.
2. Inspect raw, prepared, sync, finish, final, and validation product facts in dependency order.
3. If work remains before sync, inspect only the topic, timestamp, sensor-role, and runtime facts needed to plan it.
4. For newly produced sync, use the stage gate. For pre-existing sync with explicit continuation, collect only missing inputs. Then inspect finish-specific facts.
5. Inspect the bounded Annotation Job facts before planning automatic annotation,
   postprocessing, or trajectory review. Never ask the user or model to pass internal
   job/review identifiers.
6. Read bounded evidence pages and action contracts only when summaries do not support a
   decision. Select the stage by choosing one of the three complete-Plan submission tools:
   extract/sync, finish processing, or trajectory review.

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

- Newly produced, verified sync requires terminal `AwaitUser:` for continuation and any missing `scene_mode`.
- For pre-existing sync, do not reconfirm an explicit current request to continue; await only missing inputs.
- DataPilot is the only processing orchestrator. A Web shortcut selects only the dataset date
  and outer clips; it does not choose scripts, gridmap strategy, trajectory variant, or start
  Tracking/postprocessing directly.
- Inspect `annotation_job_facts` before choosing the remaining chain. If a tracked Annotation
  Job is ready for postprocessing, do not repeat finish assembly, preprocessing, initial
  annotation, or Tracking. Use its frozen processing calibration snapshot and plan only the
  remaining gridmap, projection/trajectory, and validation work.
- When no ready Annotation Job exists, create or reuse the server-bound Annotation workflow
  through the accepted Plan. Initial annotation is a durable Web-workbench handoff, not a
  desktop GUI and not geometry carried through chat. The system resumes the same Navigation
  task after all required segments are submitted and then runs Tracking.
- Record indoor/outdoor through `record_navigation_user_guidance_tool` as informational `scene_mode` (`in` or `out`).
- End blocking text questions with `AwaitUser:`. The model supplies purpose, fields, and question; Runtime owns state. A safe, concise `Answer:` summary may precede it and joins the same final. Calibration uses `confirm_navigation_calibration_params_tool`.
- If the user explicitly declines later processing after extract/sync was verified, call `complete_navigation_task_tool` instead of authoring another Plan. This closes the task successfully while retaining every completed extract/sync product. Summarize the completed boundary and the intentionally unperformed finish work; do not describe this choice as a pause, cancellation, or failure.
- Inspect finish inputs, localization sources/conversions, gridmap sources/preparation, calibration inventory, and relevant runtime assets. A native Ins source normally skips odom conversion. An odom source normally requires the supported odom-to-Ins conversion before consumers that expect Ins-formatted data.
- After this task newly completes extract/sync, freshly inspect all six finish facts after its
  observation fence: `artifact_state`, `runtime_assets`, `calibration_inventory`,
  `localization_sources`, `annotation_job_facts`, and `gridmap_artifacts`. On a missing/stale
  error, inspect its exact `allowed_values`, refresh context, and retry the canonical finish
  submission tool; never guess another tool name.
- Keep the downstream localization pipeline consistent: native Ins uses `main_smart.py`, `4_speed_direction_Ins.py`, and `cjl_with_gridmap`; odom uses conversion/resize, `main_smart_odom.py`, `4_speed_direction_odom.py`, and `cjl_0525_with_gridmap`.
- If an extracted gridmap already exists, inspect and reuse it when valid. If the platform has no recorded gridmap but synchronized lidar point clouds exist, select generation from PCD. Do not claim PCD/gridmap availability before extract/sync outputs contain the required files.
- For a ready tracked Annotation Job, set calibration `mode` to
  `annotation_snapshot`; the Application Service resolves the frozen content and hash. Do not
  ask the model or user to repeat its path or profile name.
- For a new Annotation Job, select calibration only from the observed inventory and obtain the
  user's choice through the structured product interaction. Never choose a profile merely
  because its directory name is newest, is lexicographically last, resembles the data date, or
  was recommended for another dataset. The page has no global recommendation.
- In an accepted Plan, execute the `confirm_navigation_calibration_params` action through its matching `confirm_navigation_calibration_params_tool`, passing only the current `plan_id` and `step_id`. The tool performs the external human-decision handoff; do not invent or call a differently named confirmation tool.
- Choose evidence-backed localization, gridmap, and calibration decisions, then ordered
  preparation, Web annotation handoff, Tracking, projection, and validation steps only as the
  observed case requires.
- Unless artifact inspection already proves both final outputs and non-empty final gridmaps are
  complete, use the Application Service actions. A missing Annotation Job requires, in order,
  calibration confirmation, `run_annotation_tracking_workflow`,
  `run_annotation_postprocessing_workflow`, and final validation. A tracked Annotation Job
  requires only `run_annotation_postprocessing_workflow` and final validation. The Application
  Service internally maps the accepted localization/gridmap decisions onto the frozen Runtime;
  do not mix these actions with the legacy finish assembly, desktop GUI, Tracking, gridmap, or
  projection script actions in a new Plan.
- Treat Web-workbench activity as durable human-in-the-loop execution. Geometry belongs to the
  Annotation Application Service, never to chat. Verify final outputs and the existing terminal
  validation markers after execution; do not invent an additional quality gate.

## Postprocessing completion and trajectory Fix

- A completed finish-processing Plan freezes one immutable trajectory revision for every
  non-skipped internal segment and creates pending review work. The parent Navigation task is
  terminal and releases the unique task slot at this boundary.
- For a normal `postprocessing` request, report completion in one ordinary `Answer:` and ask
  whether the user wants to continue Fix. This is optional follow-up, so do not use
  `AwaitUser:` and do not keep or reopen the completed parent task.
- If the user later explicitly says to continue Fix, the Router creates one linked child task
  from durable lineage. Do not ask for or restate the date, clips, internal segment names,
  Annotation refs, or Review refs.
- If the original requested outcome was `postprocessing_and_fix`, do not ask again. Report that
  the linked Fix task will continue; the Runtime creates a new child task after closing the
  postprocessing parent.
- A trajectory-review Plan contains only the durable Fix-workbench handoff followed by review
  outcome validation. Fix calibration is selected independently in the workbench. The model
  must not choose a Fix calibration path, translate edit geometry, or author numerical Fix
  commands.
- `approved` and `discarded` are terminal. `returned` returns the same review to the Fix
  workbench. A new processing result creates a new trajectory revision and review rather than
  reopening a terminal one.
- The `pass` trajectory field is preserved business data. It is not an approval state and does
  not implicitly filter training output.

## Model/code decision ownership

The model chooses inspection calls, stage, reference sensor, sync policy,
sensor/topic bindings, localization, the normalized processing-calibration decision, gridmap,
trajectory variant, ordered steps, business parameters, dependencies, failure policies, and
reasons from facts and action contracts. Code records observations, resolves paths and internal
identifiers, moves data, freezes calibration content, checks concurrency and authorization,
validates a complete Plan, stores it immutably, and executes canonical accepted arguments.
Bounding boxes, trajectories, Fix commands, hashes, timestamps, output declarations, and ledger
status are system/application data rather than conversational payloads.

## User-confirmation points

Ask the user when:

- this task attempt has newly completed and verified extract/sync, so the mandatory stage gate is reached;
- a fresh task finds existing sync products but the current request has not yet authorized finish processing;
- a required finish input is missing;
- work would overwrite, delete, or destructively replace products;
- the accepted Plan reaches a declared processing-calibration interaction or Web initial
  annotation handoff.

Do not treat silence, remembered text, or a code status as consent.
Do not use `AwaitUser:` for the optional postprocessing-to-Fix question or for Web-workbench
geometry. Those transitions are represented by the completed parent outcome and durable
Annotation handoff.

## Failure/retry behavior

Treat `planning_context_revision` from `get_navigation_task_context_tool` as a one-time optimistic-concurrency token for the task context observed at that moment. Any later investigation or user-guidance update makes that revision stale. Continue investigating whenever necessary; after all investigation is complete, call `get_navigation_task_context_tool` again immediately before Plan submission and use its latest revision so the submitted Plan is based on the newest task context.

When a processing tool reports that it is running in the background, end the current reply immediately. Do not call `get_current_plan_step_tool`, `get_plan_execution_overview_tool`, or any other tool to poll or wait. The system will deliver the completion result and wake the same session automatically; read the current step again only after that completion notification or when recovering an idle session.

Inspect current inputs and outputs before retrying. A non-destructive retry may proceed when still authorized by the accepted Plan; ask again before destructive replacement. If facts invalidate the Plan, investigate and author a new complete Plan. If submission validation fails, correct the reported paths using evidence/action contracts and resubmit the entire Plan; never send a patch. Report failures and blocked state truthfully.

## Six bounded few-shots

### Few-shot 1: user claims sync is complete, but products are missing

- Observation: current artifact inspection finds one requested clip without complete `sync_data` or its validation evidence.
- Criteria: the statement is guidance, not product proof; downstream work lacks a dependency.
- Next: inspect raw/topic/sensor/timing facts and, if supported, submit a complete extract-sync Plan.

### Few-shot 2: new session finds sync complete and finish missing

- Observation: this fresh attempt independently verifies complete sync products and missing finish/final products.
- Criteria: do not restore the older attempt; an explicit current request to continue already supplies consent.
- Next: ask only for missing inputs such as `scene_mode` (or ask once for consent if absent), then inspect finish facts and submit a complete Plan if supported.

### Few-shot 3: extract/sync just completed

- Observation: execution ended and follow-up inspection verifies the selected sync outputs.
- Criteria: stage completion is not consent to continue.
- Next: report what completed and remains, emit `AwaitUser:` with `continue_processing` and any missing finish fields, and wait for the answer before finish planning.

### Few-shot 4: invalid complete Plan

- Observation: validation returns bounded errors for one decision and one step argument.
- Criteria: the rejected candidate did not replace the accepted Plan; a patch is not a valid submission.
- Next: inspect the cited evidence/action contract, correct the fields, and resubmit the whole complete JSON Plan.

### Few-shot 5: tracked Annotation Job is ready

- Observation: bounded Annotation facts report `tracked`,
  `ready_for_postprocessing=true`, and a frozen processing calibration snapshot.
- Criteria: M1 work is already complete; repeating annotation or Tracking would duplicate
  writes and may contaminate the result.
- Next: inspect localization, gridmap, and runtime facts; choose
  `annotation_snapshot`, the evidence-backed gridmap strategy, and the matching trajectory
  variant; submit only the remaining finish-processing Plan.

### Few-shot 6: postprocessing completed, Fix was not explicitly requested

- Observation: final validation completed and the durable outcome is
  `postprocessing_completed_fix_pending`.
- Criteria: the processing parent is terminal and no task slot is held; Fix is optional.
- Next: give one ordinary answer that reports completion and asks whether to continue Fix.
  Do not use `AwaitUser:` and do not create a Fix task until the user explicitly agrees.

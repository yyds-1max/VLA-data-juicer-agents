# navigation-data-agent-planning-guidance

NavigationDataAgent planning must create a navigation WorkflowPlan from observed facts, not from date-specific rules.

Navigation processing is two-phase:

- `extract_sync`: prepare raw data and create synchronized clip artifacts. This
  phase does not require `scene_mode` or calibration confirmation.
- `finish_processing`: downstream assembly, preprocessing, annotation, tracking,
  grid map/projection, and validation. This phase requires `scene_mode` and the
  existing human calibration confirmation flow.

Planning workflow:

1. Load the session-scoped workflow plan draft.
2. Create or load the durable navigation task and reconcile it with current
   artifacts before deciding whether to rerun extract/sync or continue.
3. Inspect raw metadata topics.
4. Infer sensor bindings.
5. Infer processing_profile.
6. Infer topic_params from the role bindings.
7. For `extract_sync`, merge processing_profile, platform_hint, topic_params,
   localization_policy, calibration_policy, and the
   `extract_and_sync_navigation_data` stage variant into the draft.
8. If `scene_mode` is missing, finalize and execute only the `extract_sync`
   phase. Stop after sync completes, reconcile again, and set the task to
   `waiting_scene_mode` with `next_required_input=scene_mode`.
9. For `finish_processing`, require `scene_mode`, reconcile first, inspect
   existing processing state and grid_map artifacts, inspect runtime assets,
   list tool capabilities, and merge downstream stage variants into the draft.
10. If any blocking_issues are non-empty, stop planning and report the issues.
11. Execute only the phase-appropriate finalized WorkflowPlan.

Use the structured handoff `date` as the dataset date. Do not derive the dataset date
from clip names, because clip names may contain an older capture timestamp prefix.

Call `list_navigation_tool_capabilities_tool` before choosing finish-processing variants.
Call `update_workflow_plan_draft_tool` with the lightweight data profile.
Call `finalize_extract_sync_plan_tool` for phase 1, then
`finalize_finish_processing_plan_tool` for phase 2 after `scene_mode` is known.
`finalize_workflow_plan_tool` remains only the compatibility full-plan alias.
Do not hand-write final WorkflowPlan JSON.
If a finalized plan already exists in the session draft, use it as the durable plan reference and continue from the current AgentScope session state.

The lightweight NavigationDataProfile should summarize:

- date, segments, optional scene_mode
- processing_profile
- platform_hint
- sensor_bindings
- topic_params
- localization_policy
- calibration_policy
- gridmap_source
- projection_input_ready
- pcd_gridmap_tool_available
- stage_variants
- blocking_issues
- warnings
- evidence

Do not include full raw topic lists, calibration trees, directory inventories, or large artifact manifests in the data profile. Keep large facts in observations.
Do not invent `TOPIC_WHITELIST`, `topic_map`, or `query_dir`; copy them from `infer_navigation_topic_params_tool`.
Do not invent localization policy or calibration policy; copy them from `infer_navigation_processing_profile_tool`.
`platform_hint` is only a diagnostic hint. Do not use it as a hard selector for topic parameters, extraction variants, or projection variants.
Do not require data to fit fixed `u_legacy_like` or `go2w_like` classifications as the primary planning path.

Variant rules:

- `extract_and_sync_navigation_data` always uses the `explicit_topic_params` variant once `topic_params` is complete. Pass `topic_params.topic_whitelist`, `topic_params.topic_map`, and `topic_params.query_dir` exactly as returned by `infer_navigation_topic_params_tool`.
- `prepare_gridmap_for_projection` uses `copy_existing_gridmap` when clip/sync grid_map exists.
- `prepare_gridmap_for_projection` uses `generate_from_pcd` when no grid_map exists and the PCD gridmap tool is available.
- `prepare_gridmap_for_projection` uses `skip_if_projection_ready` when finish temp already contains projection-ready grid_map.
- `run_projection_and_trajectory` uses the explicit `projection_variant` argument from `stage_variants.run_projection_and_trajectory`. Write the same value into the WorkflowStep `variant` and the tool arguments.
- Choose `run_projection_and_trajectory` variants from observed runtime/tool capability evidence. Do not choose `cjl_0525_with_gridmap` merely because `platform_hint` is `go2w`; if no observed evidence distinguishes the projection script, use `cjl_with_gridmap`.
- The calibration confirmation gate belongs to `finish_processing`; do not run it
  during `extract_sync`. In a finish-processing plan it is the first finalized
  WorkflowPlan step. Execute that gate by calling `request_human_decision` with
  `decision_type="camera_params"`, `request_id="confirm_navigation_calibration_params:<date>"`,
  and a concise summary of the camera calibration and sensor assumptions.
- User confirmation, stop, and guidance decisions use `request_human_decision`.

Localization rules:

- If a unique Ins topic is present, set `localization_policy.source` to `ins` and `localization_policy.conversion` to `none`.
- If no unique Ins topic is present but a unique odom topic is present, set `localization_policy.source` to `odom` and `localization_policy.conversion` to `odom_to_ins`.
- If Ins is selected, NoobScenes preprocessing skips odom conversion and resize preprocessing.
- If odom is selected with `odom_to_ins`, NoobScenes preprocessing runs odom conversion and resize preprocessing.

Blocking issues:

- missing scene_mode during `finish_processing`
- missing or blocking processing_profile
- missing or blocking topic_params
- missing localization_policy
- missing gridmap source and no PCD gridmap tool
- capability catalog does not expose the selected tool variant as available

If blocking_issues is not empty, do not produce an executable plan.
If sync artifacts are missing or partial for selected segments, do not run
finish-processing tools. Reconcile the task and rerun `extract_sync` for missing
segments or ask the user how to proceed.

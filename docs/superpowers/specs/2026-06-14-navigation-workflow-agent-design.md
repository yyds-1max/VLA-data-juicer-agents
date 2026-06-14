# Navigation Workflow Agent Design

Date: 2026-06-14
Status: Draft for user review

## Goal

Build the first-stage navigation data processing agent workflow for the VLA multi-scene data processing system.

The workflow covers the company-standard navigation pipeline:

1. `prepare.sh`
2. `run_U.sh`
3. `run_odom.sh`

The workflow does not include `run_fix.sh` in this stage.

The system must use the OpenAI Agents SDK with real LLM-backed agents. The ReAct Plan-Agent and ReAct Executor-Agent must be implemented as SDK `Agent` instances run through `Runner`; they must not be replaced by a deterministic rule engine. Deterministic code is allowed only inside tools for parsing, validation, command execution, and postcondition checks.

## Source Context

Navigation processing code and sample data are currently under:

- `F:\Python_code\data-juicer-agents-main\我的数据处理流`
- `F:\Python_code\data-juicer-agents-main\我的数据处理流\VLADatasets`
- `F:\Python_code\data-juicer-agents-main\docs\导航数据处理差异情况.txt`

The company server path convention is:

- `raw_data`: raw ROS bag data
- `clip_data`: intermediate extracted and synchronized data
- `finish_data`: processed output data

Local development can override these roots, but production defaults should preserve the server conventions.

## Development Environment

Development will happen in local WSL with a project virtual environment.

The implementation plan should include:

- Create or reuse a WSL project directory for this repository.
- Create a Python virtual environment, for example `.venv`.
- Install the OpenAI Agents SDK with `pip install openai-agents`.
- Configure `OPENAI_API_KEY` in the WSL shell environment.
- Keep path configuration externalized so local Windows/WSL paths and server Linux paths can be swapped without editing business logic.

The OpenAI Agents SDK quickstart documents virtual environments, SDK installation, API key setup, `Agent`, `Runner`, function tools, multi-agent orchestration, and tracing:

- https://openai.github.io/openai-agents-python/quickstart/
- https://openai.github.io/openai-agents-python/tools/
- https://openai.github.io/openai-agents-python/tracing/

## Architecture

The first-stage navigation capability has five layers.

### Main Agent

The Main Agent talks with the user. It receives requests such as:

- Process date `20270605`.
- Process date `20270605`, only segments `20260605_152856` and `20260605_152930`.

The Main Agent delegates navigation processing to the navigation workflow capability. It does not directly call low-level data processing scripts.

### ReAct Plan-Agent

The Plan-Agent uses only read-only inspection tools and prompt context.

Its responsibilities are:

- Inspect available navigation data.
- Read raw metadata and topic information.
- Classify the dataset profile.
- Decide whether gridmap generation is required.
- Build a concrete structured processing plan from a plan template.

The Plan-Agent must be an LLM-backed OpenAI Agents SDK agent. It may use deterministic inspection tools, but the reasoning and plan synthesis must be done by the LLM.

### Structured Plan

The Plan-Agent outputs a structured plan, preferably a Pydantic model serialized to JSON.

The plan contains:

- `date`
- `segments`
- `dataset_profile`
- ordered `steps`
- tool name for each step
- tool arguments
- preconditions
- expected outputs
- whether the step blocks for human action
- failure behavior

The Executor-Agent consumes this plan rather than free-form natural language.

### ReAct Executor-Agent

The Executor-Agent uses only execution tools. It reads the structured plan and calls tools step by step.

Execution defaults to full automation. The only first-stage human interaction point is `gen_box.py`, which opens GUI windows for manual initial target annotation. The Executor-Agent must launch that tool, wait for the process to exit, inspect the generated YAML files, and then continue the remaining `run_odom.sh` stages.

The Executor-Agent must also be an LLM-backed OpenAI Agents SDK agent. It should not be replaced with a deterministic pipeline runner, although tools themselves can perform deterministic command execution and validation.

### Navigation Tools Layer

Tools wrap the existing shell and Python code behind structured function interfaces. They should preserve current processing behavior while removing interactive shell input from `prepare.sh`, `run_U.sh`, and the non-GUI parts of `run_odom.sh`.

## Dataset Profiles

Stage one supports two dataset profile families, identified from `metadata.yaml` topic combinations rather than hard-coded dates.

### `u_legacy_like`

Representative local sample: `20270515`.

Expected topics:

- Camera: `/cam_video5/csi_cam/image_raw/compressed`
- Lidar: `/lidar_points`
- Localization/Odom: `/utlidar/robot_odom_systime`

Extraction output directories:

- `cam_video5`
- `lidar_points`
- `utlidar`

Synchronization mapping:

- `cam_video5 -> fisheye_front`
- `lidar_points -> r32_rslidar_points`
- `utlidar -> odom`

The sync query directory should be selected from the actual extracted directories, typically `lidar_points` before normalization or `r32_rslidar_points` after normalization.

If no native gridmap exists, run PCD-to-gridmap generation.

### `go2w_like`

Representative local sample: `20270605`.

Expected topics:

- Camera: `/cam_video4/csi_cam/image_raw/compressed`
- Lidar: `/rs32_lidar_points`
- Localization/Odom: `/sport_odom`

Extraction output directories:

- `cam_video4`
- `rs32_lidar_points`
- `sport_odom`

Synchronization mapping:

- `cam_video4 -> fisheye_front`
- `rs32_lidar_points -> r32_rslidar_points`
- `sport_odom -> odom`

The sync query directory should be selected from the actual extracted directories, typically `rs32_lidar_points` before normalization or `r32_rslidar_points` after normalization.

If no native gridmap exists, run PCD-to-gridmap generation.

### Future Profile Placeholder

Future stages can add an Ins/gridmap-native profile for older or different platforms, for example datasets with `/drivers/ins/Ins` and native gridmap topics. This stage should keep the profile mechanism extensible but does not need to implement that full workflow.

## Read-Only Tools

The Plan-Agent can use these tools.

### `list_navigation_dates(root_kind)`

Lists date-like directories under one of:

- `raw_data`
- `clip_data`
- `finish_data`

### `inspect_raw_date(date)`

Returns:

- whether `raw_data/<date>` exists
- raw segment directory list
- metadata file locations
- topics and message counts per segment
- missing or malformed metadata

### `classify_navigation_dataset(date, segments=None)`

Classifies the dataset using topic combinations.

Returns:

- profile name
- matched topics
- missing expected topics
- confidence
- notes for the Plan-Agent

### `inspect_processing_state(date)`

Checks:

- `raw_data/<date>`
- `raw_data/<date>_temp`
- `clip_data/<date>`
- `finish_data/<date>_temp`
- `finish_data/<date>`

### `inspect_gridmap_availability(date, segments=None)`

Checks synchronized clip directories for `grid_map/*.json`.

If gridmap is missing, checks whether PCD data exists under one of:

- `r32_rslidar_points`
- `rs32_lidar_points`
- `lidar_points`

### `inspect_annotation_state(finish_temp_path)`

Checks every clip under `finish_data/<date>_temp/samples/<date>` for annotation YAML outputs:

- `master_*.yaml`
- `other*.yaml`

This is used after the human GUI annotation step.

## Execution Tools

The Executor-Agent can use these tools.

### `prepare_raw_data(date, segments=None)`

Replaces the interactive part of `prepare.sh`.

Behavior:

- Validate the date.
- Default to all raw segments when `segments` is empty.
- Validate explicit segment names when provided.
- Create `clip_data/<date>`.
- Create `raw_data/<date>_temp`.
- Symlink selected raw segment folders into `raw_data/<date>_temp`.

### `extract_and_sync_navigation_data(date, dataset_profile, segments=None, processes_num=4)`

Wraps the `run_U.sh` behavior.

Behavior:

- Use profile-specific topic whitelist and sync mapping.
- Iterate selected raw temp segment folders.
- Extract bag data into `clip_data/<date>/<segment>/tmp_dir`.
- Synchronize data into `clip_data/<date>/<segment>/sync_data`.
- Normalize sensor directory names for downstream tools.

The implementation should prefer parameterized Python wrappers over editing global constants in the original scripts.

### `generate_gridmap_from_pcd(date, segments=None)`

Runs:

`pcd_to_grid.py --base-path <clip_data_root> --date <date> --segments <segments...>`

Only run this when gridmap is missing and PCD data exists.

### `assemble_finish_temp(date, segments=None, indoor_outdoor=None)`

Wraps the data-copying and preparation part of `run_odom.sh`.

Behavior:

- Create `finish_data/<date>_temp/samples/<date>`.
- Collect synchronized clips from `clip_data/<date>/<segment>/sync_data`.
- Copy sensor calibration parameters.
- Copy `fisheye_front` and `r32_rslidar_points`.
- Preserve company output layout.

The first implementation can keep `indoor_outdoor` optional if the current two profiles do not require branching on it.

### `run_noobscene_preprocessing(finish_temp_path)`

Runs the deterministic NoobScenes preprocessing commands from `run_odom.sh`:

- `0_creat_box.py --dataset_root <finish_temp_path>`
- `1_odom_convert.py --temp_path <finish_temp_path>`
- `2_resize.py --temp_path <finish_temp_path>`
- `main_smart_odom.py`
- move generated develop data into `<finish_temp_path>/v1.0-trainval`
- copy `maps/map.png` if needed

### `run_initial_annotation_gui(finish_temp_path)`

Runs:

`gen_box.py --dataset_root <finish_temp_path>`

This tool opens the GUI, blocks until the human finishes annotation, then checks annotation YAML outputs. It is the only human-interaction step in stage one.

If no YAML outputs are found after the GUI exits, the tool fails and the Executor-Agent must stop later tracking/projection steps.

### `run_tracking_and_projection(finish_temp_path, finish_path)`

Runs the remaining non-fix `run_odom.sh` stages:

- `img2video.py --dataset_root <finish_temp_path>`
- tracking through `1_onnx_tam`
- move `tracking_img_*` and `img_*.txt` outputs to each clip
- `NuscenesAanlysis_smart_pts_project/main.py --data_root <finish_temp_path>`
- `2_pt_project/0_img2world.py <finish_temp_path>`
- `2_pt_project/4_speed_direction_odom.py <finish_temp_path>`
- `2_pt_project/2_othermethod_cjl.py <finish_temp_path>`
- `2_pt_project/3_move_dir.py --root_path <finish_path> --temp_path <finish_temp_path>`

### `validate_navigation_outputs(date)`

Checks final outputs under `finish_data/<date>` and returns a structured report.

Expected checks include:

- final date directory exists
- clip directories exist
- annotation YAML exists
- tracking output exists
- trajectory/speed output exists
- videos or expected intermediate media exist where applicable

## End-to-End Flow

For a user request with only `date`, the default is to process all raw segments under `raw_data/<date>`.

For a request with `segments`, process only those segments.

The planned step order is:

1. Inspect raw date.
2. Classify dataset profile.
3. Inspect current processing state.
4. Prepare raw temp links.
5. Extract and synchronize navigation data.
6. Inspect gridmap availability.
7. Generate gridmap from PCD when required.
8. Assemble `finish_data/<date>_temp`.
9. Run NoobScenes preprocessing.
10. Run initial annotation GUI and wait for human completion.
11. Run tracking and projection.
12. Validate final outputs.

`run_fix.sh` and trajectory-fix GUI tools are explicitly out of scope for stage one.

## Run State And Logs

Each workflow run should create a run directory, for example:

`runs/navigation/<date>/<run_id>/`

It should contain:

- `request.json`
- `inspection.json`
- `plan.json`
- `steps/<step_id>.json`
- `final_report.json`
- SDK trace identifiers or exported trace metadata when available

Each step record should include:

- tool name
- arguments
- command line if a subprocess was used
- start and end time
- return code
- stdout/stderr summary
- produced paths
- validation result

## Error Handling

The system should stop before execution if:

- date is missing
- raw directory does not exist
- metadata is missing or malformed
- no supported profile can be classified
- requested segments do not exist

The Executor-Agent should stop the plan if:

- any command returns nonzero
- expected outputs are missing
- gridmap is required but cannot be generated
- GUI annotation exits without YAML files

The stored run state should make it possible to add resume-from-step support later, but resume support is not required in the first implementation.

## Testing Strategy

### Unit Tests

Test:

- metadata parsing
- topic extraction
- profile classification
- default all-segment selection
- explicit segment selection
- gridmap required/not-required decisions
- structured plan validation

### Dry-Run Integration Tests

Using local sample directory structure, test `dry_run=True` plans for:

- `20270515`
- `20270605`

Dry-run tests should not run heavyweight ROS, OpenCV GUI, tracking, or CUDA-dependent commands. They should verify that the generated plan and commands are correct.

### Server Acceptance

On the server:

1. Create the WSL or Linux virtual environment.
2. Install dependencies.
3. Run a small selected segment.
4. Confirm `prepare`, extraction, sync, gridmap generation, finish-temp assembly, GUI annotation, tracking, projection, and final move complete.
5. Confirm final `finish_data/<date>` outputs are present.

## Non-Goals

Stage one does not:

- run `run_fix.sh`
- support all historical platform profiles
- replace human initial target annotation
- replace GUI trajectory correction
- change company data directory conventions
- implement a deterministic substitute for the Plan-Agent or Executor-Agent

## Open Questions For Implementation Planning

- Which OpenAI model should be the default for Plan-Agent and Executor-Agent in the local/server environment?
- Should subprocess tools stream stdout/stderr to the terminal while also saving logs?
- Should the first implementation create wrapper scripts beside the original processing code or copy selected logic into this repository?
- What exact server user/group ownership behavior should replace `sudo chown -R heying:heying` during local WSL development?


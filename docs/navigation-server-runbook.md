# Navigation Server Runbook

## Environment

```bash
cd /path/to/VLA-data-juicer-agents
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export VLA_AGENT_MODEL="qwen3.5-plus"
export VLA_VLADATASETS_ROOT="/media/heying/hy_data1/VLADatasets"
export VLA_PROCESSING_ROOT="/media/heying/hy_data1/Trajectory_visualization/Object_location_gh_v3_fisheye_five_U_add_SF_01"
export VLA_DATATOOLBOX_SRC="/media/heying/hy_data2/GT_dog/modules_ros2/DataToolbox/src"
export VLA_GT_DOG_ROOT="/media/heying/hy_data2/GT_dog"
export AGENT_DATA_PYTHON="/usr/bin/python3.8"
export AGENT_DATA_ENV_SETUP="/path/to/setup_data_runtime.sh"
```

The active shell and `.venv` are the Agent runtime: Python 3.12 plus AgentScope and DashScope/Qwen settings. `AGENT_DATA_PYTHON` and `AGENT_DATA_ENV_SETUP` define the legacy subprocess runtime for Python 3.8, ROS2, CUDA, GUI, and tracking tools.

## Server preflight

Before running on a server, verify the isolation boundary:

```bash
python --version
"$AGENT_DATA_PYTHON" --version
test -f "$AGENT_DATA_ENV_SETUP"
test -f "$VLA_GT_DOG_ROOT/modules/message/ros2/install/setup.bash"
test -f "$VLA_GT_DOG_ROOT/modules/ros2_ws/src/install/setup.bash"
```

Expected results: the Agent Python reports 3.12, the legacy Python reports 3.8, the configured setup script exists, and both GT_dog ROS2 setup files exist. Keep ROS, CUDA, OpenCV, Open3D, PCL, GUI, and legacy modules out of Agent imports; they belong only inside subprocess commands after the setup script has been sourced.

## Plan only

```bash
vla-nav-agent plan --date 20270605
```

## Plan dry-run without LLM

```bash
vla-nav-agent plan --date 20270605 --segments 20260605_152856 --dry-run --no-llm
```

Use this to verify the deterministic plan, selected profile, and stage-one step selection. It only generates and prints the plan; it does not execute tools and does not create wrapper command records.

## Wrapper command inspection

```bash
pytest tests/test_navigation_runtime.py tests/test_navigation_execution_tools_dry_run.py -q
```

Inspect the execution-tool dry-run assertions for wrapped legacy calls. With `AGENT_DATA_ENV_SETUP` configured, legacy Python invocations should contain `bash -lc`, `source`, and `exec "$AGENT_DATA_PYTHON"`.

## Execution dry-run

```bash
vla-nav-agent run --date 20270605 --segments 20260605_152856 --dry-run
```

This constructs and executes dry-run tools through the Executor-Agent without running the full legacy pipeline. It requires the normal LLM settings; do not add `--no-llm` to this execution command.

## Full run

```bash
vla-nav-agent run --date 20270605
```

When `gen_box.py` opens, complete the manual annotation. The workflow continues after the GUI process exits. `gen_box.py` is the only manual GUI step in stage one.

## Stage-one scope

This runbook covers `prepare.sh`, `run_U.sh`, and `run_odom.sh`. It does not run `run_fix.sh`; `run_fix.sh` remains out of scope for stage one.

## More detail

See `docs/navigation-runtime-isolation.md` for the runtime isolation model, environment variables, wrapper behavior, dry-run checks, and operational boundaries.

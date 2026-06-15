# Navigation Runtime Isolation

## Overview

The navigation agent uses two Python runtimes on purpose:

- Agent runtime: Python 3.12 with the OpenAI Agents SDK, Pydantic, pytest, the CLI, planning, dry-run orchestration, and DashScope/OpenAI-compatible client settings.
- Legacy runtime: Python 3.8 inside subprocess commands for ROS2, CUDA, OpenCV, Open3D, PCL, GUI annotation, and tracking scripts.

Keep the boundary strict. The Agent runtime must not import ROS, CUDA, OpenCV, Open3D, PCL, GUI, or legacy navigation modules, because those dependencies are tied to the Python 3.8/ROS2/CUDA environment and can pollute or crash the Python 3.12 agent process. The agent should build plans and wrapper commands; legacy code should execute only after a subprocess has entered the legacy runtime.

DashScope/OpenAI-compatible settings remain unchanged in the Agent runtime:

```bash
export DASHSCOPE_API_KEY="sk-..."
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export VLA_AGENT_MODEL="qwen3.5-plus"
```

## Runtime env vars

Set these on navigation servers before dry-run or execution:

```bash
export AGENT_DATA_PYTHON="/usr/bin/python3.8"
export AGENT_DATA_ENV_SETUP="/path/to/setup_data_runtime.sh"
export VLA_GT_DOG_ROOT="/media/heying/hy_data2/GT_dog"
```

`AGENT_DATA_PYTHON` points to the legacy Python executable used for data scripts. `AGENT_DATA_ENV_SETUP` points to the shell setup file that prepares the legacy ROS2/CUDA/GUI/tracking environment before legacy commands run. `VLA_GT_DOG_ROOT` points to the GT_dog checkout that contains the ROS2 setup files required by `run_U.sh` wrappers.

## Server preflight

Run preflight checks on the server before a real run:

```bash
python --version
"$AGENT_DATA_PYTHON" --version
test -f "$AGENT_DATA_ENV_SETUP"
test -f "$VLA_GT_DOG_ROOT/modules/message/ros2/install/setup.bash"
test -f "$VLA_GT_DOG_ROOT/modules/ros2_ws/src/install/setup.bash"
```

Expected results:

- Agent Python is 3.12.
- Legacy Python is 3.8.
- `AGENT_DATA_ENV_SETUP` exists.
- The GT_dog ROS2 setup files exist under `VLA_GT_DOG_ROOT`.

If any check fails, fix the server environment before running the workflow. Do not patch around a failed preflight by importing legacy modules into the Agent runtime.

## Command wrapper behavior

When `AGENT_DATA_ENV_SETUP` is configured, legacy Python commands are wrapped as shell commands that enter the legacy runtime:

```bash
bash -lc 'export AGENT_DATA_PYTHON=/usr/bin/python3.8 && source /path/to/setup_data_runtime.sh && exec "$AGENT_DATA_PYTHON" script.py ...'
```

`run_U.sh`-related Python commands also source the GT_dog ROS2 setup files and update `LD_LIBRARY_PATH` before the final `exec "$AGENT_DATA_PYTHON" ...` step. Non-Python legacy binaries still run in subprocesses; the Agent process only constructs and records the command.

## Plan dry-run verification

Use plan dry-run without the LLM to verify the deterministic plan, selected profile, and stage-one step selection:

```bash
vla-nav-agent plan --date 20270605 --segments 20260605_152856 --dry-run --no-llm
```

This command only generates and prints the plan. It does not execute tools and does not create wrapper command records.

## Wrapper command verification

Use tests to verify wrapped legacy command shape:

```bash
pytest tests/test_navigation_runtime.py tests/test_navigation_execution_tools_dry_run.py -q
```

In execution-tool dry-run assertions, confirm that legacy commands include `bash -lc`, `source`, and `exec "$AGENT_DATA_PYTHON"` when the setup file is configured. These checks prove the Agent Python stays separate from the legacy Python/ROS2/CUDA environment.

For an execution dry-run through the Executor-Agent, use:

```bash
vla-nav-agent run --date 20270605 --segments 20260605_152856 --dry-run
```

This constructs and executes dry-run tools without running the full legacy pipeline. It requires the normal LLM settings; do not add `--no-llm`.

## Operational boundaries

Stage one covers only `prepare.sh`, `run_U.sh`, and `run_odom.sh`. Stage one intentionally excludes `run_fix.sh`; it is out of scope and must not be run by this workflow.

`gen_box.py` is the only manual GUI step. The workflow may launch it from the legacy subprocess runtime, then waits for the human annotation session to finish before continuing.

Keep new navigation tools on the same boundary: planning, validation, records, and orchestration stay in Python 3.12; ROS2, CUDA, OpenCV, Open3D, PCL, GUI, tracking, and other legacy dependencies stay behind subprocess wrappers.

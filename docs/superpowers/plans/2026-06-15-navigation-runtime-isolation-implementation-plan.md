# Navigation Runtime Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate the Python 3.12 agent runtime from the legacy VLA data-processing runtime that requires Python 3.8, ROS2, CUDA, GUI, and tracking binaries.

**Architecture:** Keep the Agent SDK, Pydantic models, CLI, workflow, and deterministic tool orchestration in the agent Python process. Add a small runtime command builder that wraps legacy Python scripts and binaries in subprocess commands using `AGENT_DATA_PYTHON` and `AGENT_DATA_ENV_SETUP`, plus a DataToolbox-specific wrapper that sources GT_dog ROS setup and library paths. Existing tools will continue to use subprocesses, but their command construction will move through this runtime boundary.

**Tech Stack:** Python 3.12 agent runtime, Python 3.8 legacy runtime via subprocess, OpenAI Agents SDK, Pydantic, pytest, Bash setup sourcing.

---

## File Structure

- Create: `src/vla_data_juicer_agents/navigation/runtime.py`
  - Defines `NavigationRuntime`, shell quoting, `python_data_command`, `data_runtime_command`, and `run_u_python_command`.
- Modify: `src/vla_data_juicer_agents/navigation/config.py`
  - Adds `data_python`, `data_env_setup`, GT_dog paths, and runtime factory properties.
- Modify: `src/vla_data_juicer_agents/navigation/execution_tools.py`
  - Replaces direct `settings.python_bin` legacy invocations with runtime wrappers.
- Modify: `README.md`
  - Documents agent/runtime isolation variables.
- Modify: `docs/navigation-server-runbook.md`
  - Adds server-side runtime setup exports and preflight.
- Test: `tests/test_navigation_runtime.py`
  - Covers command construction and setup sourcing.
- Test: `tests/test_navigation_execution_tools_dry_run.py`
  - Updates dry-run expectations to verify runtime-wrapped command shapes.

## Task 1: Runtime Command Builders

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/runtime.py`
- Test: `tests/test_navigation_runtime.py`

- [ ] **Step 1: Write failing runtime tests**

Create `tests/test_navigation_runtime.py`:

```python
from pathlib import Path

from vla_data_juicer_agents.navigation.runtime import (
    NavigationRuntime,
    data_runtime_command,
    python_data_command,
    quote_argv,
    run_u_python_command,
)


def test_quote_argv_quotes_shell_arguments():
    assert quote_argv(["python3.8", "script path.py", "--name", "a b"]) == "python3.8 'script path.py' --name 'a b'"


def test_python_data_command_without_setup_uses_data_python():
    runtime = NavigationRuntime(data_python="/usr/bin/python3.8", data_env_setup=None)

    command = python_data_command(runtime, Path("/tools/script.py"), ["--date", "20270605"])

    assert command == ["/usr/bin/python3.8", "/tools/script.py", "--date", "20270605"]


def test_python_data_command_with_setup_sources_legacy_env():
    runtime = NavigationRuntime(
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    command = python_data_command(runtime, Path("/tools/script.py"), ["--date", "20270605"])

    assert command[:2] == ["bash", "-lc"]
    assert "export AGENT_DATA_PYTHON=/usr/bin/python3.8" in command[2]
    assert "source /env/setup_data_runtime.sh" in command[2]
    assert 'exec "$AGENT_DATA_PYTHON" /tools/script.py --date 20270605' in command[2]


def test_data_runtime_command_sources_setup_for_binary():
    runtime = NavigationRuntime(
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    command = data_runtime_command(runtime, ["./bin/main"])

    assert command == ["bash", "-lc", "source /env/setup_data_runtime.sh && exec ./bin/main"]


def test_run_u_python_command_sources_ros_and_shm_paths():
    runtime = NavigationRuntime(
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    command = run_u_python_command(
        runtime,
        script_name="1_extract_data_from_bag_multi_process_ros2_U.py",
        args=["--data_path", "/raw"],
        ros2_setup_bash=Path("/gt/modules/message/ros2/install/setup.bash"),
        ros2_ws_setup_bash=Path("/gt/modules/ros2_ws/src/install/setup.bash"),
        shm_msgs_lib_dir=Path("/gt/modules/message/shm/install/shm_msgs/lib"),
    )

    assert command[:2] == ["bash", "-lc"]
    shell = command[2]
    assert "source /env/setup_data_runtime.sh" in shell
    assert "source /gt/modules/message/ros2/install/setup.bash" in shell
    assert "source /gt/modules/ros2_ws/src/install/setup.bash" in shell
    assert "/gt/modules/message/shm/install/shm_msgs/lib" in shell
    assert 'exec "$AGENT_DATA_PYTHON" 1_extract_data_from_bag_multi_process_ros2_U.py --data_path /raw' in shell
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_runtime.py -q
```

Expected: FAIL because `navigation.runtime` does not exist.

- [ ] **Step 3: Implement `runtime.py`**

Create `src/vla_data_juicer_agents/navigation/runtime.py`:

```python
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class NavigationRuntime:
    data_python: str
    data_env_setup: Path | None = None


def quote_argv(argv: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in argv)


def python_data_command(runtime: NavigationRuntime, script_path: str | Path, args: Sequence[str]) -> list[str]:
    script_args = [str(script_path), *[str(arg) for arg in args]]
    if runtime.data_env_setup is None:
        return [str(runtime.data_python), *script_args]

    setup = shlex.quote(str(runtime.data_env_setup))
    data_python = shlex.quote(str(runtime.data_python))
    shell = (
        f"export AGENT_DATA_PYTHON={data_python} && "
        f"source {setup} && "
        f'exec "$AGENT_DATA_PYTHON" {quote_argv(script_args)}'
    )
    return ["bash", "-lc", shell]


def data_runtime_command(runtime: NavigationRuntime, argv: Sequence[str]) -> list[str]:
    command_argv = [str(part) for part in argv]
    if runtime.data_env_setup is None:
        return command_argv

    setup = shlex.quote(str(runtime.data_env_setup))
    return ["bash", "-lc", f"source {setup} && exec {quote_argv(command_argv)}"]


def run_u_python_command(
    runtime: NavigationRuntime,
    script_name: str,
    args: Sequence[str],
    ros2_setup_bash: Path,
    ros2_ws_setup_bash: Path,
    shm_msgs_lib_dir: Path,
) -> list[str]:
    command_argv = [script_name, *[str(arg) for arg in args]]
    setup_lines: list[str] = []
    if runtime.data_env_setup is not None:
        setup_lines.extend(
            [
                f"export AGENT_DATA_PYTHON={shlex.quote(str(runtime.data_python))}",
                f"source {shlex.quote(str(runtime.data_env_setup))}",
            ]
        )
        exec_line = f'exec "$AGENT_DATA_PYTHON" {quote_argv(command_argv)}'
    else:
        exec_line = quote_argv([str(runtime.data_python), *command_argv])

    lines = [
        *setup_lines,
        "export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}",
        "export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}:"
        f"{shlex.quote(str(shm_msgs_lib_dir))}",
        f"source {shlex.quote(str(ros2_setup_bash))}",
        f"source {shlex.quote(str(ros2_ws_setup_bash))}",
        exec_line,
    ]
    return ["bash", "-lc", " && ".join(lines)]
```

- [ ] **Step 4: Run runtime tests**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_runtime.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/navigation/runtime.py tests/test_navigation_runtime.py
git commit -m "feat: add navigation legacy runtime commands"
```

## Task 2: Runtime Settings

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/config.py`
- Test: `tests/test_navigation_runtime.py`

- [ ] **Step 1: Write failing settings tests**

Append to `tests/test_navigation_runtime.py`:

```python
from vla_data_juicer_agents.navigation.config import NavigationSettings


def test_navigation_settings_reads_legacy_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_PYTHON", "/usr/bin/python3.8")
    monkeypatch.setenv("AGENT_DATA_ENV_SETUP", str(tmp_path / "setup_data_runtime.sh"))
    monkeypatch.setenv("VLA_GT_DOG_ROOT", str(tmp_path / "GT_dog"))

    settings = NavigationSettings()

    assert settings.runtime.data_python == "/usr/bin/python3.8"
    assert settings.runtime.data_env_setup == tmp_path / "setup_data_runtime.sh"
    assert settings.ros2_setup_bash == tmp_path / "GT_dog" / "modules" / "message" / "ros2" / "install" / "setup.bash"
    assert settings.ros2_ws_setup_bash == tmp_path / "GT_dog" / "modules" / "ros2_ws" / "src" / "install" / "setup.bash"
    assert settings.shm_msgs_lib_dir == tmp_path / "GT_dog" / "modules" / "message" / "shm" / "install" / "shm_msgs" / "lib"
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_runtime.py -q
```

Expected: FAIL because `NavigationSettings.runtime` and GT_dog path properties do not exist.

- [ ] **Step 3: Update `config.py`**

Add:

```python
from vla_data_juicer_agents.navigation.runtime import NavigationRuntime

DEFAULT_GT_DOG_ROOT = Path("/media/heying/hy_data2/GT_dog")

def _optional_env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None
```

Add fields/properties to `NavigationSettings`:

```python
    data_python: str = Field(default_factory=lambda: os.getenv("AGENT_DATA_PYTHON", "python3"))
    data_env_setup: Path | None = Field(default_factory=lambda: _optional_env_path("AGENT_DATA_ENV_SETUP"))
    gt_dog_root: Path = Field(default_factory=lambda: _env_path("VLA_GT_DOG_ROOT", DEFAULT_GT_DOG_ROOT))

    @property
    def runtime(self) -> NavigationRuntime:
        return NavigationRuntime(data_python=self.data_python, data_env_setup=self.data_env_setup)

    @property
    def ros2_setup_bash(self) -> Path:
        return self.gt_dog_root / "modules" / "message" / "ros2" / "install" / "setup.bash"

    @property
    def ros2_ws_setup_bash(self) -> Path:
        return self.gt_dog_root / "modules" / "ros2_ws" / "src" / "install" / "setup.bash"

    @property
    def shm_msgs_lib_dir(self) -> Path:
        return self.gt_dog_root / "modules" / "message" / "shm" / "install" / "shm_msgs" / "lib"
```

Do not remove `python_bin` yet; it remains useful for agent-side tests and backward compatibility until all call sites are migrated.

- [ ] **Step 4: Run tests**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_runtime.py tests/test_navigation_workflow_models.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/navigation/config.py tests/test_navigation_runtime.py
git commit -m "feat: configure navigation legacy runtime"
```

## Task 3: Migrate Execution Tools To Runtime Wrappers

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/execution_tools.py`
- Test: `tests/test_navigation_execution_tools_dry_run.py`

- [ ] **Step 1: Write failing execution command tests**

Append to `tests/test_navigation_execution_tools_dry_run.py`:

```python
def test_gridmap_uses_legacy_python_runtime_setup(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=Path("/processing"),
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    result = generate_gridmap_from_pcd("20270605", ["20260605_152856"], settings=settings, dry_run=True)

    command = result.commands[0].command
    assert command[:2] == ["bash", "-lc"]
    assert "source /env/setup_data_runtime.sh" in command[2]
    assert 'exec "$AGENT_DATA_PYTHON" /processing/other_code/pcd_to_grid.py' in command[2]


def test_run_u_extract_uses_ros_runtime_wrapper(tmp_path):
    root = tmp_path / "VLADatasets"
    (root / "raw_data" / "20270605_temp" / "20260605_152856").mkdir(parents=True)
    settings = NavigationSettings(
        vladatasets_root=root,
        datatoolbox_src=Path("/toolbox"),
        gt_dog_root=Path("/gt"),
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    result = extract_and_sync_navigation_data("20270605", "go2w_like", settings=settings, dry_run=True)

    command = result.commands[0].command
    assert command[:2] == ["bash", "-lc"]
    assert "source /env/setup_data_runtime.sh" in command[2]
    assert "source /gt/modules/message/ros2/install/setup.bash" in command[2]
    assert "source /gt/modules/ros2_ws/src/install/setup.bash" in command[2]
    assert "1_extract_data_from_bag_multi_process_ros2_U.py" in command[2]


def test_tracking_binary_uses_data_runtime_setup(tmp_path):
    root = tmp_path / "VLADatasets"
    finish_temp = root / "finish_data" / "20270605_temp"
    clip = finish_temp / "samples" / "20270605" / "20260605_152856_zhigu_wuhan_0"
    clip.mkdir(parents=True)
    (clip / "master_red_blue_black.yaml").write_text("{}", encoding="utf-8")
    settings = NavigationSettings(
        vladatasets_root=root,
        processing_root=Path("/processing"),
        data_python="/usr/bin/python3.8",
        data_env_setup=Path("/env/setup_data_runtime.sh"),
    )

    result = run_tracking_and_projection(
        "finish_data/20270605_temp",
        "finish_data/20270605",
        settings=settings,
        dry_run=True,
    )

    tracking_commands = [record.command for record in result.commands if "./bin/main" in record.command[-1]]
    assert tracking_commands
    assert tracking_commands[0][:2] == ["bash", "-lc"]
    assert "source /env/setup_data_runtime.sh && exec ./bin/main" in tracking_commands[0][2]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_execution_tools_dry_run.py -q
```

Expected: FAIL because current commands directly use `settings.python_bin` or `./bin/main`.

- [ ] **Step 3: Update execution command construction**

In `execution_tools.py`, import:

```python
from vla_data_juicer_agents.navigation.runtime import (
    data_runtime_command,
    python_data_command,
    run_u_python_command,
)
```

Replace command construction:

- `generate_gridmap_from_pcd`: use `python_data_command(settings.runtime, settings.pcd_to_grid_script, [...])`.
- `extract_and_sync_navigation_data`: use `run_u_python_command(settings.runtime, script_name, args, settings.ros2_setup_bash, settings.ros2_ws_setup_bash, settings.shm_msgs_lib_dir)` for both extract and sync commands. Keep `cwd=settings.datatoolbox_src`.
- `run_noobscene_preprocessing`: use `python_data_command(...)` for `0_creat_box.py`, `1_odom_convert.py`, `2_resize.py`, and `main_smart_odom.py`. For `main_smart_odom.py`, pass script path `./main_smart_odom.py` and `cwd=noobscene_root`.
- `run_initial_annotation_gui`: use `python_data_command(...)` for `settings.gen_box_script`.
- `run_tracking_and_projection`: use `python_data_command(...)` for `img2video.py`, projection scripts, and `data_runtime_command(settings.runtime, ["./bin/main"])` for tracking binary.

Keep dry-run non-mutating.

- [ ] **Step 4: Run focused tests**

Run:

```bash
./.venv/bin/pytest tests/test_navigation_execution_tools_dry_run.py tests/test_navigation_runtime.py -q
```

Expected: PASS.

- [ ] **Step 5: Run full suite**

Run:

```bash
./.venv/bin/pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/navigation/execution_tools.py tests/test_navigation_execution_tools_dry_run.py
git commit -m "feat: run navigation tools through legacy runtime"
```

## Task 4: Documentation And Server Runbook

**Files:**
- Modify: `README.md`
- Modify: `docs/navigation-server-runbook.md`
- Create: `docs/navigation-runtime-isolation.md`

- [ ] **Step 1: Add runtime isolation docs**

Create `docs/navigation-runtime-isolation.md`:

```markdown
# Navigation Runtime Isolation

The agent runtime and VLA data-processing runtime are intentionally separate.

The agent runtime runs Python 3.10+ / 3.12 and imports only orchestration dependencies:

- OpenAI Agents SDK
- Pydantic
- CLI and workflow code
- deterministic tool contracts

The data-processing runtime runs legacy server dependencies through subprocesses:

- Python 3.8
- ROS2
- CUDA
- OpenCV / Open3D / PCL
- GUI scripts
- tracking binaries

Configure the legacy runtime:

```bash
export AGENT_DATA_PYTHON="/usr/bin/python3.8"
export AGENT_DATA_ENV_SETUP="/media/heying/hy_data2/GT_dog/env/setup_data_runtime.sh"
export VLA_GT_DOG_ROOT="/media/heying/hy_data2/GT_dog"
```

The navigation tools never import legacy processing modules into the agent process.
They build subprocess commands that source `AGENT_DATA_ENV_SETUP` and then execute
`AGENT_DATA_PYTHON` or the tracking binary.

Use dry-run commands locally to inspect the command shape before server execution:

```bash
vla-nav-agent plan --date 20270605 --segments 20260605_152856 --dry-run --no-llm
```
```

- [ ] **Step 2: Update README**

Append a short section:

```markdown
## Runtime isolation

The agent Python environment is separate from the legacy VLA data-processing runtime.
Set `AGENT_DATA_PYTHON` and `AGENT_DATA_ENV_SETUP` on the server so ROS2/CUDA tools
run in the legacy subprocess environment. See `docs/navigation-runtime-isolation.md`.
```

- [ ] **Step 3: Update server runbook environment**

Add these exports to `docs/navigation-server-runbook.md`:

```bash
export AGENT_DATA_PYTHON="/usr/bin/python3.8"
export AGENT_DATA_ENV_SETUP="/media/heying/hy_data2/GT_dog/env/setup_data_runtime.sh"
export VLA_GT_DOG_ROOT="/media/heying/hy_data2/GT_dog"
```

Add a preflight:

```bash
python --version
bash -lc 'source "$AGENT_DATA_ENV_SETUP" && "$AGENT_DATA_PYTHON" --version'
```

- [ ] **Step 4: Run docs grep**

Run:

```bash
grep -R "AGENT_DATA_PYTHON\\|AGENT_DATA_ENV_SETUP" README.md docs/navigation-server-runbook.md docs/navigation-runtime-isolation.md
```

Expected: all three docs mention runtime isolation variables.

- [ ] **Step 5: Run full tests**

Run:

```bash
./.venv/bin/pytest -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```bash
git add README.md docs/navigation-server-runbook.md docs/navigation-runtime-isolation.md
git commit -m "docs: document navigation runtime isolation"
```

## Task 5: Final Verification

**Files:**
- All implementation files

- [ ] **Step 1: Run full tests**

Run:

```bash
./.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Verify dry-run plans still work**

Run:

```bash
VLA_RUNS_ROOT=/tmp/vla-nav-runtime-runs \
VLA_VLADATASETS_ROOT="$(pwd)/tests/fixtures/navigation/VLADatasets" \
./.venv/bin/vla-nav-agent plan --date 20270515 --dry-run --no-llm
```

Expected:

- output contains `"dataset_profile": "u_legacy_like"`
- output contains `"tool_name": "run_initial_annotation_gui"`
- output does not contain `run_fix`

Run:

```bash
VLA_RUNS_ROOT=/tmp/vla-nav-runtime-runs \
VLA_VLADATASETS_ROOT="$(pwd)/tests/fixtures/navigation/VLADatasets" \
./.venv/bin/vla-nav-agent plan --date 20270605 --dry-run --no-llm
```

Expected:

- output contains `"dataset_profile": "go2w_like"`
- output contains `"tool_name": "generate_gridmap_from_pcd"`
- output contains `"human_blocking": true`
- output does not contain `run_fix`

- [ ] **Step 3: Verify command wrappers with env setup**

Run a focused test:

```bash
./.venv/bin/pytest tests/test_navigation_runtime.py tests/test_navigation_execution_tools_dry_run.py -q
```

Expected: PASS.

- [ ] **Step 4: Final git status**

Run:

```bash
git status --short
```

Expected: only pre-existing dirty design/plan docs remain, unless the user has changed other files.

## Self-Review Checklist

- Runtime separation: Agent Python no longer directly chooses the legacy Python executable via `python_bin`; legacy scripts use `AGENT_DATA_PYTHON`.
- Environment setup: commands source `AGENT_DATA_ENV_SETUP` when configured.
- DataToolbox U scripts: commands source GT_dog ROS setup and shm lib path.
- C++ tracking binary: command uses `data_runtime_command`.
- Dry-run remains non-mutating.
- No `run_fix.sh` is introduced.
- Docs explain server setup and preflight.

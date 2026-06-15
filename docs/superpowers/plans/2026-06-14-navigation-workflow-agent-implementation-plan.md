# Navigation Workflow Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first-stage Qwen-backed navigation workflow agent that plans and executes `prepare.sh -> run_U.sh -> run_odom.sh` for the `20270515` and `20270605` data families.

**Architecture:** Create a focused Python package with OpenAI Agents SDK agents, deterministic function tools, Pydantic workflow models, path/profile configuration, subprocess wrappers, and dry-run tests. The Plan-Agent and Executor-Agent are real SDK `Agent` instances run through `Runner`, using Alibaba Cloud DashScope Qwen through an OpenAI-compatible Chat Completions model object; deterministic rules live only inside tools for inspection, validation, command construction, and postcondition checks.

**Tech Stack:** Python 3.11+, OpenAI Agents SDK (`openai-agents`), OpenAI Python client for compatible endpoints, Alibaba Cloud DashScope Qwen (`qwen3.5-plus` by default), Pydantic, PyYAML, pytest, WSL virtual environment, existing ROS/navigation scripts.

---

## File Structure

Create this project structure:

```text
pyproject.toml
README.md
src/vla_data_juicer_agents/__init__.py
src/vla_data_juicer_agents/cli.py
src/vla_data_juicer_agents/navigation/__init__.py
src/vla_data_juicer_agents/navigation/agents.py
src/vla_data_juicer_agents/navigation/config.py
src/vla_data_juicer_agents/navigation/execution_tools.py
src/vla_data_juicer_agents/navigation/inspection.py
src/vla_data_juicer_agents/navigation/models.py
src/vla_data_juicer_agents/navigation/profiles.py
src/vla_data_juicer_agents/navigation/run_state.py
src/vla_data_juicer_agents/navigation/subprocess_runner.py
src/vla_data_juicer_agents/navigation/workflow.py
tests/fixtures/navigation/VLADatasets/raw_data/20270515/20260515_102948/metadata.yaml
tests/fixtures/navigation/VLADatasets/raw_data/20270605/20260605_152856/metadata.yaml
tests/test_navigation_inspection.py
tests/test_navigation_profiles.py
tests/test_navigation_workflow_models.py
tests/test_navigation_execution_tools_dry_run.py
tests/test_navigation_agents.py
```

Responsibilities:

- `config.py`: path settings and script locations.
- `models.py`: Pydantic input/output models shared by agents and tools.
- `profiles.py`: supported dataset profiles and topic mappings.
- `inspection.py`: deterministic read-only inspection functions and SDK function tools.
- `execution_tools.py`: deterministic execution and dry-run function tools.
- `subprocess_runner.py`: command execution, dry-run command records, stdout/stderr capture.
- `run_state.py`: persisted workflow run artifacts.
- `agents.py`: OpenAI Agents SDK `Agent` definitions.
- `workflow.py`: orchestration helpers that run the Plan-Agent and Executor-Agent.
- `cli.py`: command-line entrypoint for WSL/server use.

## Task 1: Project Skeleton And WSL Environment

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `src/vla_data_juicer_agents/__init__.py`
- Create: `src/vla_data_juicer_agents/navigation/__init__.py`

- [ ] **Step 1: Create package directories**

Run:

```bash
mkdir -p src/vla_data_juicer_agents/navigation tests/fixtures/navigation/VLADatasets/raw_data/20270515/20260515_102948 tests/fixtures/navigation/VLADatasets/raw_data/20270605/20260605_152856
touch src/vla_data_juicer_agents/__init__.py
touch src/vla_data_juicer_agents/navigation/__init__.py
```

Expected: directories and empty package files exist.

- [ ] **Step 2: Create `pyproject.toml`**

Add:

```toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "vla-data-juicer-agents"
version = "0.1.0"
description = "LLM-backed navigation data processing agents for VLA data workflows"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "openai>=1.0.0",
    "openai-agents>=0.2.0",
    "pydantic>=2.7",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.2",
]

[project.scripts]
vla-nav-agent = "vla_data_juicer_agents.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

- [ ] **Step 3: Create `README.md`**

Add:

```markdown
# VLA Data Juicer Agents

This project builds an OpenAI Agents SDK workflow for the first-stage navigation data pipeline:

1. prepare raw ROS bag segment links
2. extract and synchronize navigation data
3. generate gridmap from PCD when needed
4. assemble `finish_data/<date>_temp`
5. run `run_odom.sh` stages through initial annotation, tracking, projection, and final move

Stage one intentionally excludes `run_fix.sh`.

## WSL setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export VLA_AGENT_MODEL="qwen3.5-plus"
```

## Dry run

```bash
vla-nav-agent plan --date 20270605 --dry-run
```

## Execute

```bash
vla-nav-agent run --date 20270605
```
```

- [ ] **Step 4: Create WSL virtual environment**

Run in WSL from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected: install succeeds. If network access is unavailable, record the failure and install later on the server or a network-enabled WSL shell.

- [ ] **Step 5: Run empty test suite**

Run:

```bash
source .venv/bin/activate
pytest -q
```

Expected: no tests collected or all current tests pass.

- [ ] **Step 6: Commit skeleton if this directory is a git repository**

Run:

```bash
git status --short
git add pyproject.toml README.md src/vla_data_juicer_agents tests
git commit -m "chore: scaffold navigation agent project"
```

Expected: commit succeeds if git is initialized. If this directory is not a git repository, record `git status` output and continue without committing.

## Task 2: Configuration And Shared Models

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/config.py`
- Create: `src/vla_data_juicer_agents/navigation/models.py`
- Test: `tests/test_navigation_workflow_models.py`

- [ ] **Step 1: Write failing model tests**

Create `tests/test_navigation_workflow_models.py`:

```python
import pytest

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep


def test_navigation_request_defaults_to_all_segments():
    request = NavigationRequest(date="20270605")

    assert request.date == "20270605"
    assert request.segments is None
    assert request.dry_run is False


def test_navigation_request_rejects_bad_date():
    with pytest.raises(ValueError):
        NavigationRequest(date="2026-06-05")


def test_navigation_settings_derives_data_roots(tmp_path):
    settings = NavigationSettings(vladatasets_root=tmp_path / "VLADatasets")

    assert settings.raw_data_root == tmp_path / "VLADatasets" / "raw_data"
    assert settings.clip_data_root == tmp_path / "VLADatasets" / "clip_data"
    assert settings.finish_data_root == tmp_path / "VLADatasets" / "finish_data"


def test_workflow_plan_keeps_ordered_steps():
    plan = WorkflowPlan(
        date="20270605",
        segments=["20260605_152856"],
        dataset_profile="go2w_like",
        steps=[
            WorkflowStep(
                step_id="prepare_raw_data",
                tool_name="prepare_raw_data",
                arguments={"date": "20270605", "segments": ["20260605_152856"]},
                expected_outputs=["raw_data/20270605_temp/20260605_152856"],
            )
        ],
    )

    assert plan.steps[0].tool_name == "prepare_raw_data"
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_navigation_workflow_models.py -q
```

Expected: FAIL because `config.py` and `models.py` do not exist.

- [ ] **Step 3: Implement `config.py`**

Create `src/vla_data_juicer_agents/navigation/config.py`:

```python
from __future__ import annotations

import os
from pathlib import Path
from pydantic import BaseModel, Field


DEFAULT_VLADATASETS_ROOT = Path("/media/heying/hy_data1/VLADatasets")
DEFAULT_PROCESSING_ROOT = Path("/media/heying/hy_data1/Trajectory_visualization/Object_location_gh_v3_fisheye_five_U_add_SF_01")
DEFAULT_DATATOOLBOX_SRC = Path("/media/heying/hy_data2/GT_dog/modules_ros2/DataToolbox/src")


class NavigationSettings(BaseModel):
    """Path settings for local WSL and server execution."""

    vladatasets_root: Path = Field(default_factory=lambda: Path(os.getenv("VLA_VLADATASETS_ROOT", DEFAULT_VLADATASETS_ROOT)))
    processing_root: Path = Field(default_factory=lambda: Path(os.getenv("VLA_PROCESSING_ROOT", DEFAULT_PROCESSING_ROOT)))
    datatoolbox_src: Path = Field(default_factory=lambda: Path(os.getenv("VLA_DATATOOLBOX_SRC", DEFAULT_DATATOOLBOX_SRC)))
    runs_root: Path = Field(default_factory=lambda: Path(os.getenv("VLA_RUNS_ROOT", "runs/navigation")))
    python_bin: str = Field(default_factory=lambda: os.getenv("VLA_PYTHON_BIN", "python3"))
    process_owner: str | None = Field(default_factory=lambda: os.getenv("VLA_PROCESS_OWNER"))

    @property
    def raw_data_root(self) -> Path:
        return self.vladatasets_root / "raw_data"

    @property
    def clip_data_root(self) -> Path:
        return self.vladatasets_root / "clip_data"

    @property
    def finish_data_root(self) -> Path:
        return self.vladatasets_root / "finish_data"

    @property
    def pcd_to_grid_script(self) -> Path:
        return self.processing_root / "other_code" / "pcd_to_grid.py"

    @property
    def gen_box_script(self) -> Path:
        return self.processing_root / "0_1th_box" / "gen_box.py"
```

- [ ] **Step 4: Implement `models.py`**

Create `src/vla_data_juicer_agents/navigation/models.py`:

```python
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


DATE_RE = re.compile(r"^[0-9]{8}$")


class NavigationRequest(BaseModel):
    date: str
    segments: list[str] | None = None
    dry_run: bool = False

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        if not DATE_RE.match(value):
            raise ValueError("date must use YYYYMMDD format")
        return value


class TopicInfo(BaseModel):
    name: str
    type: str | None = None
    message_count: int = 0


class SegmentInspection(BaseModel):
    name: str
    path: Path
    metadata_path: Path | None = None
    topics: list[TopicInfo] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RawDateInspection(BaseModel):
    date: str
    path: Path
    exists: bool
    segments: list[SegmentInspection] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ProfileClassification(BaseModel):
    profile_name: str | None
    confidence: float
    matched_topics: list[str] = Field(default_factory=list)
    missing_topics: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class WorkflowStep(BaseModel):
    step_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    human_blocking: bool = False
    failure_behavior: str = "stop"


class WorkflowPlan(BaseModel):
    date: str
    segments: list[str] | None = None
    dataset_profile: str
    steps: list[WorkflowStep]

    @field_validator("date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        if not DATE_RE.match(value):
            raise ValueError("date must use YYYYMMDD format")
        return value


class CommandRecord(BaseModel):
    command: list[str]
    cwd: Path | None = None
    dry_run: bool = False
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""


class ToolResult(BaseModel):
    ok: bool
    tool_name: str
    message: str
    produced_paths: list[Path] = Field(default_factory=list)
    commands: list[CommandRecord] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class RunStatus(BaseModel):
    status: Literal["planned", "running", "failed", "completed"]
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
```

- [ ] **Step 5: Run model tests**

Run:

```bash
pytest tests/test_navigation_workflow_models.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run if git is available:

```bash
git add src/vla_data_juicer_agents/navigation/config.py src/vla_data_juicer_agents/navigation/models.py tests/test_navigation_workflow_models.py
git commit -m "feat: add navigation workflow models"
```

## Task 3: Dataset Profiles And Metadata Inspection

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/profiles.py`
- Create: `src/vla_data_juicer_agents/navigation/inspection.py`
- Create fixture: `tests/fixtures/navigation/VLADatasets/raw_data/20270515/20260515_102948/metadata.yaml`
- Create fixture: `tests/fixtures/navigation/VLADatasets/raw_data/20270605/20260605_152856/metadata.yaml`
- Test: `tests/test_navigation_inspection.py`
- Test: `tests/test_navigation_profiles.py`

- [ ] **Step 1: Add metadata fixtures**

Create `tests/fixtures/navigation/VLADatasets/raw_data/20270515/20260515_102948/metadata.yaml`:

```yaml
rosbag2_bagfile_information:
  version: 4
  storage_identifier: sqlite3
  relative_file_paths:
    - 20260515_102948_0.db3
  duration:
    nanoseconds: 34355090864
  starting_time:
    nanoseconds_since_epoch: 1778812189469693651
  message_count: 15574
  topics_with_message_count:
    - topic_metadata:
        name: /sport_imu
        type: sensor_msgs/msg/Imu
      message_count: 9761
    - topic_metadata:
        name: /lidar_points
        type: sensor_msgs/msg/PointCloud2
      message_count: 334
    - topic_metadata:
        name: /cam_video5/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 343
    - topic_metadata:
        name: /utlidar/robot_odom_systime
        type: nav_msgs/msg/Odometry
      message_count: 5136
```

Create `tests/fixtures/navigation/VLADatasets/raw_data/20270605/20260605_152856/metadata.yaml`:

```yaml
rosbag2_bagfile_information:
  version: 5
  storage_identifier: sqlite3
  relative_file_paths:
    - 20260605_152856_0.db3
  duration:
    nanoseconds: 21872824365
  starting_time:
    nanoseconds_since_epoch: 1780644537605923086
  message_count: 17655
  topics_with_message_count:
    - topic_metadata:
        name: /cam_video4/csi_cam/image_raw/compressed
        type: sensor_msgs/msg/CompressedImage
      message_count: 219
    - topic_metadata:
        name: /rs32_lidar_points
        type: sensor_msgs/msg/PointCloud2
      message_count: 218
    - topic_metadata:
        name: /sport_imu
        type: sensor_msgs/msg/Imu
      message_count: 10910
    - topic_metadata:
        name: /sport_odom
        type: nav_msgs/msg/Odometry
      message_count: 6308
```

- [ ] **Step 2: Write failing profile tests**

Create `tests/test_navigation_profiles.py`:

```python
from vla_data_juicer_agents.navigation.profiles import classify_topics, get_profile


def test_classifies_20270515_topic_family():
    topics = {
        "/cam_video5/csi_cam/image_raw/compressed",
        "/lidar_points",
        "/utlidar/robot_odom_systime",
    }

    result = classify_topics(topics)

    assert result.profile_name == "u_legacy_like"
    assert result.confidence == 1.0


def test_classifies_20270605_topic_family():
    topics = {
        "/cam_video4/csi_cam/image_raw/compressed",
        "/rs32_lidar_points",
        "/sport_odom",
    }

    result = classify_topics(topics)

    assert result.profile_name == "go2w_like"
    assert result.confidence == 1.0


def test_profile_contains_sync_mapping():
    profile = get_profile("go2w_like")

    assert profile.sync_topic_map["cam_video4"] == "fisheye_front"
    assert profile.sync_topic_map["rs32_lidar_points"] == "r32_rslidar_points"
    assert profile.sync_topic_map["sport_odom"] == "odom"
```

- [ ] **Step 3: Write failing inspection tests**

Create `tests/test_navigation_inspection.py`:

```python
from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.inspection import inspect_raw_date, list_navigation_dates


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "navigation" / "VLADatasets"


def test_list_navigation_dates_finds_raw_dates():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    dates = list_navigation_dates("raw_data", settings=settings)

    assert dates == ["20270515", "20270605"]


def test_inspect_raw_date_reads_topics():
    settings = NavigationSettings(vladatasets_root=FIXTURE_ROOT)

    result = inspect_raw_date("20270605", settings=settings)

    assert result.exists is True
    assert result.segments[0].name == "20260605_152856"
    topic_names = {topic.name for topic in result.segments[0].topics}
    assert "/cam_video4/csi_cam/image_raw/compressed" in topic_names
    assert "/sport_odom" in topic_names
```

- [ ] **Step 4: Run tests to verify failure**

Run:

```bash
pytest tests/test_navigation_profiles.py tests/test_navigation_inspection.py -q
```

Expected: FAIL because profile and inspection modules do not exist.

- [ ] **Step 5: Implement `profiles.py`**

Create `src/vla_data_juicer_agents/navigation/profiles.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from vla_data_juicer_agents.navigation.models import ProfileClassification


@dataclass(frozen=True)
class NavigationProfile:
    name: str
    required_topics: frozenset[str]
    extract_topics: tuple[str, ...]
    sync_topic_map: dict[str, str]
    lidar_dirs: tuple[str, ...]


PROFILES: dict[str, NavigationProfile] = {
    "u_legacy_like": NavigationProfile(
        name="u_legacy_like",
        required_topics=frozenset(
            {
                "/cam_video5/csi_cam/image_raw/compressed",
                "/lidar_points",
                "/utlidar/robot_odom_systime",
            }
        ),
        extract_topics=(
            "/cam_video5/csi_cam/image_raw/compressed",
            "/lidar_points",
            "/utlidar/robot_odom_systime",
        ),
        sync_topic_map={
            "cam_video5": "fisheye_front",
            "lidar_points": "r32_rslidar_points",
            "utlidar": "odom",
        },
        lidar_dirs=("r32_rslidar_points", "lidar_points"),
    ),
    "go2w_like": NavigationProfile(
        name="go2w_like",
        required_topics=frozenset(
            {
                "/cam_video4/csi_cam/image_raw/compressed",
                "/rs32_lidar_points",
                "/sport_odom",
            }
        ),
        extract_topics=(
            "/cam_video4/csi_cam/image_raw/compressed",
            "/rs32_lidar_points",
            "/sport_odom",
        ),
        sync_topic_map={
            "cam_video4": "fisheye_front",
            "rs32_lidar_points": "r32_rslidar_points",
            "sport_odom": "odom",
        },
        lidar_dirs=("r32_rslidar_points", "rs32_lidar_points"),
    ),
}


def get_profile(name: str) -> NavigationProfile:
    return PROFILES[name]


def classify_topics(topics: set[str]) -> ProfileClassification:
    best_name: str | None = None
    best_score = 0.0
    best_missing: list[str] = []
    best_matched: list[str] = []

    for profile in PROFILES.values():
        matched = sorted(profile.required_topics.intersection(topics))
        missing = sorted(profile.required_topics.difference(topics))
        score = len(matched) / len(profile.required_topics)
        if score > best_score:
            best_name = profile.name
            best_score = score
            best_missing = missing
            best_matched = matched

    if best_score < 1.0:
        return ProfileClassification(
            profile_name=None,
            confidence=best_score,
            matched_topics=best_matched,
            missing_topics=best_missing,
            notes=["No supported profile matched all required topics."],
        )

    return ProfileClassification(
        profile_name=best_name,
        confidence=best_score,
        matched_topics=best_matched,
        missing_topics=[],
    )
```

- [ ] **Step 6: Implement `inspection.py`**

Create `src/vla_data_juicer_agents/navigation/inspection.py`:

```python
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
import yaml

from agents import function_tool

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import RawDateInspection, SegmentInspection, TopicInfo
from vla_data_juicer_agents.navigation.profiles import classify_topics


DATE_RE = re.compile(r"^[0-9]{8}$")


def _root_for(root_kind: Literal["raw_data", "clip_data", "finish_data"], settings: NavigationSettings) -> Path:
    if root_kind == "raw_data":
        return settings.raw_data_root
    if root_kind == "clip_data":
        return settings.clip_data_root
    return settings.finish_data_root


def list_navigation_dates(root_kind: Literal["raw_data", "clip_data", "finish_data"], settings: NavigationSettings | None = None) -> list[str]:
    settings = settings or NavigationSettings()
    root = _root_for(root_kind, settings)
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir() and DATE_RE.match(path.name))


def _parse_metadata(metadata_path: Path) -> list[TopicInfo]:
    with metadata_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    info = payload.get("rosbag2_bagfile_information", {})
    topic_entries = info.get("topics_with_message_count", [])
    topics: list[TopicInfo] = []
    for entry in topic_entries:
        metadata = entry.get("topic_metadata", {})
        topics.append(
            TopicInfo(
                name=metadata.get("name", ""),
                type=metadata.get("type"),
                message_count=int(entry.get("message_count") or 0),
            )
        )
    return topics


def inspect_raw_date(date: str, settings: NavigationSettings | None = None) -> RawDateInspection:
    settings = settings or NavigationSettings()
    raw_date_path = settings.raw_data_root / date
    result = RawDateInspection(date=date, path=raw_date_path, exists=raw_date_path.exists())
    if not raw_date_path.exists():
        result.errors.append(f"Raw date directory does not exist: {raw_date_path}")
        return result

    for segment_path in sorted(path for path in raw_date_path.iterdir() if path.is_dir()):
        metadata_path = segment_path / "metadata.yaml"
        segment = SegmentInspection(name=segment_path.name, path=segment_path, metadata_path=metadata_path if metadata_path.exists() else None)
        if not metadata_path.exists():
            segment.errors.append(f"Missing metadata.yaml in {segment_path}")
        else:
            try:
                segment.topics = _parse_metadata(metadata_path)
            except Exception as exc:
                segment.errors.append(f"Failed to parse metadata.yaml: {exc}")
        result.segments.append(segment)
    return result


def classify_navigation_dataset(date: str, segments: list[str] | None = None, settings: NavigationSettings | None = None):
    inspection = inspect_raw_date(date, settings=settings)
    selected = inspection.segments
    if segments:
        segment_set = set(segments)
        selected = [segment for segment in selected if segment.name in segment_set]
    topics = {topic.name for segment in selected for topic in segment.topics}
    return classify_topics(topics)


@function_tool
def list_navigation_dates_tool(root_kind: Literal["raw_data", "clip_data", "finish_data"]) -> list[str]:
    """List available date directories under a navigation data root."""
    return list_navigation_dates(root_kind)


@function_tool
def inspect_raw_date_tool(date: str) -> dict:
    """Inspect raw navigation data for a date, including segments and ROS topics."""
    return inspect_raw_date(date).model_dump(mode="json")


@function_tool
def classify_navigation_dataset_tool(date: str, segments: list[str] | None = None) -> dict:
    """Classify a navigation dataset profile from raw metadata topics."""
    return classify_navigation_dataset(date, segments).model_dump()
```

- [ ] **Step 7: Run inspection/profile tests**

Run:

```bash
pytest tests/test_navigation_profiles.py tests/test_navigation_inspection.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

Run if git is available:

```bash
git add src/vla_data_juicer_agents/navigation/profiles.py src/vla_data_juicer_agents/navigation/inspection.py tests/fixtures tests/test_navigation_profiles.py tests/test_navigation_inspection.py
git commit -m "feat: inspect navigation metadata and classify profiles"
```

## Task 4: Structured Plan Builder And Plan-Agent

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/agents.py`
- Create: `src/vla_data_juicer_agents/navigation/workflow.py`
- Test: `tests/test_navigation_agents.py`

- [ ] **Step 1: Write failing agent tests**

Create `tests/test_navigation_agents.py`:

```python
from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template


def test_create_plan_agent_has_read_only_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_plan_agent()
    tool_names = {tool.name for tool in agent.tools}

    assert "inspect_raw_date_tool" in tool_names
    assert "classify_navigation_dataset_tool" in tool_names


def test_create_executor_agent_has_execution_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    agent = create_executor_agent(dry_run=True)
    tool_names = {tool.name for tool in agent.tools}

    assert "prepare_raw_data_tool" in tool_names
    assert "run_initial_annotation_gui_tool" in tool_names


def test_plan_template_includes_human_gui_step():
    plan = build_deterministic_plan_template(date="20270605", dataset_profile="go2w_like", segments=None)

    gui_steps = [step for step in plan.steps if step.tool_name == "run_initial_annotation_gui"]
    assert len(gui_steps) == 1
    assert gui_steps[0].human_blocking is True
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_navigation_agents.py -q
```

Expected: FAIL because agents and execution tools do not exist yet.

- [ ] **Step 3: Add plan template in `workflow.py`**

Create `src/vla_data_juicer_agents/navigation/workflow.py`:

```python
from __future__ import annotations

import json
from agents import Runner

from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep


def build_deterministic_plan_template(date: str, dataset_profile: str, segments: list[str] | None) -> WorkflowPlan:
    """Build the required step skeleton; the LLM Plan-Agent still decides and emits the final plan."""
    common_args = {"date": date, "segments": segments}
    finish_temp_path = f"finish_data/{date}_temp"
    finish_path = f"finish_data/{date}"
    return WorkflowPlan(
        date=date,
        segments=segments,
        dataset_profile=dataset_profile,
        steps=[
            WorkflowStep(step_id="prepare", tool_name="prepare_raw_data", arguments=common_args, expected_outputs=[f"raw_data/{date}_temp"]),
            WorkflowStep(step_id="extract_sync", tool_name="extract_and_sync_navigation_data", arguments={**common_args, "dataset_profile": dataset_profile}, expected_outputs=[f"clip_data/{date}"]),
            WorkflowStep(step_id="gridmap", tool_name="generate_gridmap_from_pcd", arguments=common_args, expected_outputs=[f"clip_data/{date}/*/sync_data/*/grid_map"], failure_behavior="skip_if_gridmap_exists"),
            WorkflowStep(step_id="assemble_finish_temp", tool_name="assemble_finish_temp", arguments=common_args, expected_outputs=[finish_temp_path]),
            WorkflowStep(step_id="noobscene_preprocess", tool_name="run_noobscene_preprocessing", arguments={"finish_temp_path": finish_temp_path}, expected_outputs=[f"{finish_temp_path}/v1.0-trainval"]),
            WorkflowStep(step_id="annotation_gui", tool_name="run_initial_annotation_gui", arguments={"finish_temp_path": finish_temp_path}, expected_outputs=[f"{finish_temp_path}/samples/{date}/*/*.yaml"], human_blocking=True),
            WorkflowStep(step_id="tracking_projection", tool_name="run_tracking_and_projection", arguments={"finish_temp_path": finish_temp_path, "finish_path": finish_path}, expected_outputs=[finish_path]),
            WorkflowStep(step_id="validate", tool_name="validate_navigation_outputs", arguments={"date": date}, expected_outputs=[finish_path]),
        ],
    )


async def run_plan_agent(agent, request: NavigationRequest) -> WorkflowPlan:
    prompt = (
        "Inspect the navigation dataset and return a JSON WorkflowPlan. "
        "Use tools to inspect metadata and classify the dataset. "
        "Stage one covers prepare.sh, run_U.sh, and run_odom.sh only. "
        "Do not include run_fix.sh. "
        f"Request: {request.model_dump_json()}"
    )
    result = await Runner.run(agent, prompt)
    return WorkflowPlan.model_validate_json(result.final_output)


async def run_executor_agent(agent, plan: WorkflowPlan) -> str:
    prompt = (
        "Execute this WorkflowPlan step by step using the available execution tools. "
        "Stop on tool failure. The gen_box GUI step is human-blocking; wait for it to finish. "
        f"Plan JSON: {json.dumps(plan.model_dump(mode='json'), ensure_ascii=False)}"
    )
    result = await Runner.run(agent, prompt)
    return str(result.final_output)
```

- [ ] **Step 4: Add temporary execution tool stubs so agent construction is testable**

This step will be replaced by full execution tools in Task 5. Create `src/vla_data_juicer_agents/navigation/execution_tools.py`:

```python
from __future__ import annotations

from agents import function_tool


@function_tool
def prepare_raw_data_tool(date: str, segments: list[str] | None = None) -> dict:
    """Prepare raw data symlinks for selected navigation segments."""
    return {"ok": True, "dry_run": True, "date": date, "segments": segments}


@function_tool
def extract_and_sync_navigation_data_tool(date: str, dataset_profile: str, segments: list[str] | None = None, processes_num: int = 4) -> dict:
    """Extract and synchronize navigation data."""
    return {"ok": True, "dry_run": True, "date": date, "dataset_profile": dataset_profile, "segments": segments, "processes_num": processes_num}


@function_tool
def generate_gridmap_from_pcd_tool(date: str, segments: list[str] | None = None) -> dict:
    """Generate gridmap JSON files from synchronized PCD files."""
    return {"ok": True, "dry_run": True, "date": date, "segments": segments}


@function_tool
def assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
    """Assemble finish_data temp layout from synchronized clip data."""
    return {"ok": True, "dry_run": True, "date": date, "segments": segments}


@function_tool
def run_noobscene_preprocessing_tool(finish_temp_path: str) -> dict:
    """Run NoobScenes preprocessing commands."""
    return {"ok": True, "dry_run": True, "finish_temp_path": finish_temp_path}


@function_tool
def run_initial_annotation_gui_tool(finish_temp_path: str) -> dict:
    """Run gen_box.py GUI and wait for manual annotation completion."""
    return {"ok": True, "dry_run": True, "finish_temp_path": finish_temp_path}


@function_tool
def run_tracking_and_projection_tool(finish_temp_path: str, finish_path: str) -> dict:
    """Run tracking, projection, speed, trajectory, and final move steps."""
    return {"ok": True, "dry_run": True, "finish_temp_path": finish_temp_path, "finish_path": finish_path}


@function_tool
def validate_navigation_outputs_tool(date: str) -> dict:
    """Validate final navigation outputs."""
    return {"ok": True, "dry_run": True, "date": date}
```

- [ ] **Step 5: Implement `agents.py` with real SDK agents**

Create `src/vla_data_juicer_agents/navigation/agents.py`:

```python
from __future__ import annotations

import os

from agents import Agent, OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI

from vla_data_juicer_agents.navigation.execution_tools import (
    assemble_finish_temp_tool,
    extract_and_sync_navigation_data_tool,
    generate_gridmap_from_pcd_tool,
    prepare_raw_data_tool,
    run_initial_annotation_gui_tool,
    run_noobscene_preprocessing_tool,
    run_tracking_and_projection_tool,
    validate_navigation_outputs_tool,
)
from vla_data_juicer_agents.navigation.inspection import (
    classify_navigation_dataset_tool,
    inspect_raw_date_tool,
    list_navigation_dates_tool,
)


PLAN_INSTRUCTIONS = """
You are the ReAct Plan-Agent for VLA navigation data processing.
Use only read-only tools to inspect the requested data.
Return only a valid WorkflowPlan JSON object.
Stage one covers prepare.sh, run_U.sh, and run_odom.sh.
Do not include run_fix.sh.
The only human-blocking step is gen_box.py via run_initial_annotation_gui.
Default to all raw segments when the user did not specify segments.
Supported profiles are u_legacy_like and go2w_like.
"""


EXECUTOR_INSTRUCTIONS = """
You are the ReAct Executor-Agent for VLA navigation data processing.
Read the WorkflowPlan JSON and execute each step with the matching tool.
Stop after a failed tool result.
The run_initial_annotation_gui tool opens a GUI and blocks until the human finishes.
Never invent filesystem results; rely on tool outputs.
"""


def create_qwen_model(model: str | None = None) -> OpenAIChatCompletionsModel:
    api_key = os.environ["DASHSCOPE_API_KEY"]
    base_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    model_name = model or os.getenv("VLA_AGENT_MODEL", "qwen3.5-plus")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def create_plan_agent(model: str | None = None) -> Agent:
    set_tracing_disabled(True)
    return Agent(
        name="Navigation ReAct Plan-Agent",
        instructions=PLAN_INSTRUCTIONS,
        model=create_qwen_model(model),
        tools=[
            list_navigation_dates_tool,
            inspect_raw_date_tool,
            classify_navigation_dataset_tool,
        ],
    )


def create_executor_agent(model: str | None = None, dry_run: bool = False) -> Agent:
    set_tracing_disabled(True)
    return Agent(
        name="Navigation ReAct Executor-Agent",
        instructions=f"{EXECUTOR_INSTRUCTIONS}\nDry run mode: {dry_run}",
        model=create_qwen_model(model),
        tools=[
            prepare_raw_data_tool,
            extract_and_sync_navigation_data_tool,
            generate_gridmap_from_pcd_tool,
            assemble_finish_temp_tool,
            run_noobscene_preprocessing_tool,
            run_initial_annotation_gui_tool,
            run_tracking_and_projection_tool,
            validate_navigation_outputs_tool,
        ],
    )
```

- [ ] **Step 6: Run agent tests**

Run:

```bash
pytest tests/test_navigation_agents.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run if git is available:

```bash
git add src/vla_data_juicer_agents/navigation/agents.py src/vla_data_juicer_agents/navigation/workflow.py src/vla_data_juicer_agents/navigation/execution_tools.py tests/test_navigation_agents.py
git commit -m "feat: add navigation SDK agents"
```

## Task 5: Subprocess Runner And Dry-Run Execution Tools

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/subprocess_runner.py`
- Modify: `src/vla_data_juicer_agents/navigation/execution_tools.py`
- Test: `tests/test_navigation_execution_tools_dry_run.py`

- [ ] **Step 1: Write failing dry-run tests**

Create `tests/test_navigation_execution_tools_dry_run.py`:

```python
from pathlib import Path

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.execution_tools import (
    generate_gridmap_from_pcd,
    prepare_raw_data,
)


def test_prepare_raw_data_dry_run_defaults_to_all_segments(tmp_path):
    root = tmp_path / "VLADatasets"
    raw_date = root / "raw_data" / "20270605"
    (raw_date / "20260605_152856").mkdir(parents=True)
    (raw_date / "20260605_152930").mkdir()
    settings = NavigationSettings(vladatasets_root=root)

    result = prepare_raw_data("20270605", settings=settings, dry_run=True)

    assert result.ok is True
    assert "20260605_152856" in result.details["selected_segments"]
    assert "20260605_152930" in result.details["selected_segments"]


def test_generate_gridmap_from_pcd_dry_run_builds_command(tmp_path):
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "VLADatasets",
        processing_root=Path("/processing"),
    )

    result = generate_gridmap_from_pcd("20270605", ["20260605_152856"], settings=settings, dry_run=True)

    command = result.commands[0].command
    assert command[:2] == ["python3", "/processing/other_code/pcd_to_grid.py"]
    assert "--date" in command
    assert "20270605" in command
    assert "--segments" in command
    assert "20260605_152856" in command
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
pytest tests/test_navigation_execution_tools_dry_run.py -q
```

Expected: FAIL because deterministic execution functions are not implemented.

- [ ] **Step 3: Implement `subprocess_runner.py`**

Create `src/vla_data_juicer_agents/navigation/subprocess_runner.py`:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

from vla_data_juicer_agents.navigation.models import CommandRecord


def run_command(command: list[str], cwd: Path | None = None, dry_run: bool = False, timeout_seconds: int | None = None) -> CommandRecord:
    if dry_run:
        return CommandRecord(command=command, cwd=cwd, dry_run=True)

    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandRecord(
        command=command,
        cwd=cwd,
        dry_run=False,
        return_code=completed.returncode,
        stdout=completed.stdout[-8000:],
        stderr=completed.stderr[-8000:],
    )
```

- [ ] **Step 4: Replace `execution_tools.py` stubs with deterministic functions plus SDK wrappers**

Replace `src/vla_data_juicer_agents/navigation/execution_tools.py` with:

```python
from __future__ import annotations

import shutil
from pathlib import Path
from agents import function_tool

from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.models import ToolResult
from vla_data_juicer_agents.navigation.profiles import get_profile
from vla_data_juicer_agents.navigation.subprocess_runner import run_command


def _selected_segments(raw_date_path: Path, segments: list[str] | None) -> list[str]:
    if not raw_date_path.exists():
        raise FileNotFoundError(f"Raw date directory does not exist: {raw_date_path}")
    available = sorted(path.name for path in raw_date_path.iterdir() if path.is_dir())
    if segments is None:
        return available
    missing = sorted(set(segments).difference(available))
    if missing:
        raise FileNotFoundError(f"Requested segments do not exist: {missing}")
    return segments


def prepare_raw_data(date: str, segments: list[str] | None = None, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    raw_date_path = settings.raw_data_root / date
    selected = _selected_segments(raw_date_path, segments)
    raw_temp_path = settings.raw_data_root / f"{date}_temp"
    clip_date_path = settings.clip_data_root / date

    produced = [raw_temp_path, clip_date_path]
    if not dry_run:
        clip_date_path.mkdir(parents=True, exist_ok=True)
        raw_temp_path.mkdir(parents=True, exist_ok=True)
        for segment in selected:
            source = raw_date_path / segment
            target = raw_temp_path / segment
            if target.exists() or target.is_symlink():
                continue
            target.symlink_to(source, target_is_directory=True)

    return ToolResult(
        ok=True,
        tool_name="prepare_raw_data",
        message=f"Prepared {len(selected)} raw segments for {date}.",
        produced_paths=produced,
        details={"selected_segments": selected, "dry_run": dry_run},
    )


def generate_gridmap_from_pcd(date: str, segments: list[str] | None = None, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    command = [
        settings.python_bin,
        str(settings.pcd_to_grid_script),
        "--base-path",
        str(settings.clip_data_root),
        "--date",
        date,
    ]
    if segments:
        command.append("--segments")
        command.extend(segments)
    record = run_command(command, dry_run=dry_run)
    ok = dry_run or record.return_code == 0
    return ToolResult(
        ok=ok,
        tool_name="generate_gridmap_from_pcd",
        message="Generated gridmap from PCD." if ok else "Gridmap generation failed.",
        commands=[record],
        produced_paths=[settings.clip_data_root / date],
        details={"dry_run": dry_run},
    )


def extract_and_sync_navigation_data(date: str, dataset_profile: str, segments: list[str] | None = None, processes_num: int = 4, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    profile = get_profile(dataset_profile)
    raw_temp_path = settings.raw_data_root / f"{date}_temp"
    selected = _selected_segments(raw_temp_path, segments)
    commands = []
    for segment in selected:
        data_path = raw_temp_path / segment
        save_path = settings.clip_data_root / date / segment
        extract_script = settings.datatoolbox_src / "1_extract_data_from_bag_multi_process_ros2_U.py"
        sync_script = settings.datatoolbox_src / "2_sync_data_multi_process_U.py"
        commands.append(run_command([settings.python_bin, str(extract_script), "--data_path", str(data_path), "--save_path", str(save_path), "--processes_num", str(processes_num)], cwd=settings.datatoolbox_src, dry_run=dry_run))
        query_dir = profile.lidar_dirs[-1]
        commands.append(run_command([settings.python_bin, str(sync_script), "--data_path", str(save_path), "--query_dir", query_dir, "--output_dir", "sync_data", "--sequence_prefix", f"{segment}_zhigu_wuhan", "--processes_num", str(processes_num)], cwd=settings.datatoolbox_src, dry_run=dry_run))
    ok = all(command.dry_run or command.return_code == 0 for command in commands)
    return ToolResult(ok=ok, tool_name="extract_and_sync_navigation_data", message="Extracted and synchronized navigation data." if ok else "Extract/sync failed.", commands=commands, produced_paths=[settings.clip_data_root / date], details={"profile": profile.name, "segments": selected, "dry_run": dry_run})


def assemble_finish_temp(date: str, segments: list[str] | None = None, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    clip_date_root = settings.clip_data_root / date
    finish_temp = settings.finish_data_root / f"{date}_temp"
    samples_date_root = finish_temp / "samples" / date
    selected = _selected_segments(clip_date_root, segments)
    sensor_source = settings.processing_root / "NoobScenes" / "params" / "20260409_U" / "sensors"
    copied_clips: list[str] = []

    for segment in selected:
        sync_root = clip_date_root / segment / "sync_data"
        if not sync_root.exists():
            raise FileNotFoundError(f"Missing sync_data directory: {sync_root}")
        for src_clip in sorted(path for path in sync_root.iterdir() if path.is_dir()):
            dst_clip = samples_date_root / src_clip.name
            copied_clips.append(src_clip.name)
            if dry_run:
                continue
            dst_clip.mkdir(parents=True, exist_ok=True)
            if sensor_source.exists():
                shutil.copytree(sensor_source, dst_clip / "sensors", dirs_exist_ok=True)
            for child_name in ("fisheye_front", "r32_rslidar_points", "grid_map", "odom"):
                src_child = src_clip / child_name
                if src_child.exists():
                    shutil.copytree(src_child, dst_clip / child_name, dirs_exist_ok=True)

    if not dry_run:
        samples_date_root.mkdir(parents=True, exist_ok=True)
    return ToolResult(
        ok=True,
        tool_name="assemble_finish_temp",
        message=f"Assembled {len(copied_clips)} clips into finish temp layout.",
        produced_paths=[finish_temp],
        details={"date": date, "segments": selected, "copied_clips": copied_clips, "dry_run": dry_run},
    )


def run_noobscene_preprocessing(finish_temp_path: str, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    root = Path(finish_temp_path)
    commands = [
        run_command([settings.python_bin, str(settings.processing_root / "NoobScenes" / "include" / "0_creat_box.py"), "--dataset_root", str(root)], dry_run=dry_run),
        run_command([settings.python_bin, str(settings.processing_root / "NoobScenes" / "include" / "1_odom_convert.py"), "--temp_path", str(root)], dry_run=dry_run),
        run_command([settings.python_bin, str(settings.processing_root / "NoobScenes" / "include" / "2_resize.py"), "--temp_path", str(root)], dry_run=dry_run),
    ]
    ok = all(command.dry_run or command.return_code == 0 for command in commands)
    return ToolResult(ok=ok, tool_name="run_noobscene_preprocessing", message="Ran NoobScenes preprocessing." if ok else "NoobScenes preprocessing failed.", commands=commands, produced_paths=[root / "v1.0-trainval"], details={"dry_run": dry_run})


def run_initial_annotation_gui(finish_temp_path: str, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    root = Path(finish_temp_path)
    record = run_command([settings.python_bin, str(settings.gen_box_script), "--dataset_root", str(root)], dry_run=dry_run)
    ok = dry_run or record.return_code == 0
    yaml_count = 0 if dry_run else len(list((root / "samples").glob("*/*/*.yaml")))
    if not dry_run and yaml_count == 0:
        ok = False
    return ToolResult(ok=ok, tool_name="run_initial_annotation_gui", message="Annotation GUI completed." if ok else "Annotation GUI did not produce YAML files.", commands=[record], produced_paths=[root], details={"dry_run": dry_run, "yaml_count": yaml_count})


def run_tracking_and_projection(finish_temp_path: str, finish_path: str, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    root = Path(finish_temp_path)
    final = Path(finish_path)
    commands = [
        run_command([settings.python_bin, str(settings.processing_root / "0_1th_box" / "img2video.py"), "--dataset_root", str(root)], dry_run=dry_run),
        run_command([settings.python_bin, "main.py", "--data_root", str(root)], cwd=settings.processing_root / "NuscenesAanlysis_smart_pts_project", dry_run=dry_run),
        run_command([settings.python_bin, str(settings.processing_root / "2_pt_project" / "0_img2world.py"), str(root)], cwd=settings.processing_root / "2_pt_project", dry_run=dry_run),
        run_command([settings.python_bin, str(settings.processing_root / "2_pt_project" / "4_speed_direction_odom.py"), str(root)], cwd=settings.processing_root / "2_pt_project", dry_run=dry_run),
        run_command([settings.python_bin, str(settings.processing_root / "2_pt_project" / "2_othermethod_cjl.py"), str(root)], cwd=settings.processing_root / "2_pt_project", dry_run=dry_run),
        run_command([settings.python_bin, str(settings.processing_root / "2_pt_project" / "3_move_dir.py"), "--root_path", str(final), "--temp_path", str(root)], cwd=settings.processing_root / "2_pt_project", dry_run=dry_run),
    ]
    ok = all(command.dry_run or command.return_code == 0 for command in commands)
    return ToolResult(ok=ok, tool_name="run_tracking_and_projection", message="Ran tracking and projection." if ok else "Tracking/projection failed.", commands=commands, produced_paths=[final], details={"dry_run": dry_run})


def validate_navigation_outputs(date: str, settings: NavigationSettings | None = None, dry_run: bool = False) -> ToolResult:
    settings = settings or NavigationSettings()
    final = settings.finish_data_root / date
    exists = final.exists()
    ok = dry_run or exists
    return ToolResult(ok=ok, tool_name="validate_navigation_outputs", message="Validation completed." if ok else f"Missing final output: {final}", produced_paths=[final], details={"exists": exists, "dry_run": dry_run})


@function_tool
def prepare_raw_data_tool(date: str, segments: list[str] | None = None) -> dict:
    return prepare_raw_data(date, segments).model_dump(mode="json")


@function_tool
def extract_and_sync_navigation_data_tool(date: str, dataset_profile: str, segments: list[str] | None = None, processes_num: int = 4) -> dict:
    return extract_and_sync_navigation_data(date, dataset_profile, segments, processes_num).model_dump(mode="json")


@function_tool
def generate_gridmap_from_pcd_tool(date: str, segments: list[str] | None = None) -> dict:
    return generate_gridmap_from_pcd(date, segments).model_dump(mode="json")


@function_tool
def assemble_finish_temp_tool(date: str, segments: list[str] | None = None) -> dict:
    return assemble_finish_temp(date, segments).model_dump(mode="json")


@function_tool
def run_noobscene_preprocessing_tool(finish_temp_path: str) -> dict:
    return run_noobscene_preprocessing(finish_temp_path).model_dump(mode="json")


@function_tool
def run_initial_annotation_gui_tool(finish_temp_path: str) -> dict:
    return run_initial_annotation_gui(finish_temp_path).model_dump(mode="json")


@function_tool
def run_tracking_and_projection_tool(finish_temp_path: str, finish_path: str) -> dict:
    return run_tracking_and_projection(finish_temp_path, finish_path).model_dump(mode="json")


@function_tool
def validate_navigation_outputs_tool(date: str) -> dict:
    return validate_navigation_outputs(date).model_dump(mode="json")
```

- [ ] **Step 5: Run dry-run execution tests**

Run:

```bash
pytest tests/test_navigation_execution_tools_dry_run.py -q
```

Expected: PASS.

- [ ] **Step 6: Run all tests**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run if git is available:

```bash
git add src/vla_data_juicer_agents/navigation/subprocess_runner.py src/vla_data_juicer_agents/navigation/execution_tools.py tests/test_navigation_execution_tools_dry_run.py
git commit -m "feat: add dry-run navigation execution tools"
```

## Task 6: Run State Persistence

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/run_state.py`
- Test: `tests/test_navigation_run_state.py`

- [ ] **Step 1: Write failing run-state tests**

Create `tests/test_navigation_run_state.py`:

```python
from vla_data_juicer_agents.navigation.models import NavigationRequest, WorkflowPlan, WorkflowStep
from vla_data_juicer_agents.navigation.run_state import WorkflowRunStore


def test_workflow_run_store_writes_request_and_plan(tmp_path):
    store = WorkflowRunStore(root=tmp_path)
    request = NavigationRequest(date="20270605")
    plan = WorkflowPlan(
        date="20270605",
        dataset_profile="go2w_like",
        steps=[WorkflowStep(step_id="prepare", tool_name="prepare_raw_data")],
    )

    run_dir = store.create_run("20270605")
    store.write_json(run_dir, "request.json", request.model_dump(mode="json"))
    store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))

    assert (run_dir / "request.json").exists()
    assert (run_dir / "plan.json").exists()
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
pytest tests/test_navigation_run_state.py -q
```

Expected: FAIL because `run_state.py` does not exist.

- [ ] **Step 3: Implement `run_state.py`**

Create `src/vla_data_juicer_agents/navigation/run_state.py`:

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WorkflowRunStore:
    def __init__(self, root: Path):
        self.root = root

    def create_run(self, date: str) -> Path:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = self.root / date / run_id
        (run_dir / "steps").mkdir(parents=True, exist_ok=False)
        return run_dir

    def write_json(self, run_dir: Path, relative_name: str, payload: dict[str, Any]) -> Path:
        path = run_dir / relative_name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        return path
```

- [ ] **Step 4: Run run-state test**

Run:

```bash
pytest tests/test_navigation_run_state.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run if git is available:

```bash
git add src/vla_data_juicer_agents/navigation/run_state.py tests/test_navigation_run_state.py
git commit -m "feat: persist navigation workflow run state"
```

## Task 7: CLI And End-To-End Dry Run

**Files:**
- Create: `src/vla_data_juicer_agents/cli.py`
- Modify: `src/vla_data_juicer_agents/navigation/workflow.py`
- Test: `tests/test_navigation_cli.py`

- [ ] **Step 1: Write failing CLI test**

Create `tests/test_navigation_cli.py`:

```python
from vla_data_juicer_agents.cli import parse_args


def test_parse_plan_dry_run_args():
    args = parse_args(["plan", "--date", "20270605", "--segments", "20260605_152856", "--dry-run"])

    assert args.command == "plan"
    assert args.date == "20270605"
    assert args.segments == ["20260605_152856"]
    assert args.dry_run is True
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
pytest tests/test_navigation_cli.py -q
```

Expected: FAIL because `cli.py` does not exist.

- [ ] **Step 3: Implement `cli.py`**

Create `src/vla_data_juicer_agents/cli.py`:

```python
from __future__ import annotations

import argparse
import asyncio
import json

from vla_data_juicer_agents.navigation.agents import create_executor_agent, create_plan_agent
from vla_data_juicer_agents.navigation.inspection import classify_navigation_dataset
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.workflow import build_deterministic_plan_template, run_executor_agent, run_plan_agent


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="VLA navigation workflow agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("plan", "run"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--date", required=True)
        sub.add_argument("--segments", nargs="*", default=None)
        sub.add_argument("--dry-run", action="store_true")
        sub.add_argument("--model", default=None, help="Qwen model id; defaults to VLA_AGENT_MODEL or qwen3.5-plus.")
        sub.add_argument("--no-llm", action="store_true", help="Only build the deterministic plan template for local dry-run debugging.")

    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    request = NavigationRequest(date=args.date, segments=args.segments, dry_run=args.dry_run)

    if args.no_llm:
        classification = classify_navigation_dataset(request.date, request.segments)
        if not classification.profile_name:
            print(json.dumps(classification.model_dump(), indent=2, ensure_ascii=False))
            return 2
        plan = build_deterministic_plan_template(request.date, classification.profile_name, request.segments)
    else:
        plan_agent = create_plan_agent(model=args.model)
        plan = await run_plan_agent(plan_agent, request)

    if args.command == "plan":
        print(plan.model_dump_json(indent=2))
        return 0

    executor_agent = create_executor_agent(model=args.model, dry_run=args.dry_run)
    final_output = await run_executor_agent(executor_agent, plan)
    print(final_output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))
```

- [ ] **Step 4: Run CLI unit test**

Run:

```bash
pytest tests/test_navigation_cli.py -q
```

Expected: PASS.

- [ ] **Step 5: Run deterministic local dry-run against fixtures**

Run:

```bash
VLA_VLADATASETS_ROOT="$(pwd)/tests/fixtures/navigation/VLADatasets" vla-nav-agent plan --date 20270605 --dry-run --no-llm
```

Expected: command prints a `WorkflowPlan` JSON with `dataset_profile` set to `go2w_like` and a human-blocking `run_initial_annotation_gui` step.

- [ ] **Step 6: Run full test suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 7: Commit**

Run if git is available:

```bash
git add src/vla_data_juicer_agents/cli.py src/vla_data_juicer_agents/navigation/workflow.py tests/test_navigation_cli.py
git commit -m "feat: add navigation workflow CLI"
```

## Task 8: Server Execution Readiness

**Files:**
- Modify: `README.md`
- Create: `docs/navigation-server-runbook.md`

- [ ] **Step 1: Add server runbook**

Create `docs/navigation-server-runbook.md`:

```markdown
# Navigation Server Runbook

## Environment

```bash
cd /path/to/VLA-data-juicer-agents
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export DASHSCOPE_API_KEY="sk-..."
export DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export VLA_AGENT_MODEL="qwen3.5-plus"
export VLA_VLADATASETS_ROOT="/media/heying/hy_data1/VLADatasets"
export VLA_PROCESSING_ROOT="/media/heying/hy_data1/Trajectory_visualization/Object_location_gh_v3_fisheye_five_U_add_SF_01"
export VLA_DATATOOLBOX_SRC="/media/heying/hy_data2/GT_dog/modules_ros2/DataToolbox/src"
```

## Plan only

```bash
vla-nav-agent plan --date 20270605
```

## Dry-run without LLM for command inspection

```bash
vla-nav-agent plan --date 20270605 --segments 20260605_152856 --dry-run --no-llm
```

## Full run

```bash
vla-nav-agent run --date 20270605
```

When `gen_box.py` opens, complete the manual annotation. The workflow continues after the GUI process exits.

## Stage-one scope

This runbook covers `prepare.sh`, `run_U.sh`, and `run_odom.sh`. It does not run `run_fix.sh`.
```

- [ ] **Step 2: Link runbook from `README.md`**

Append:

```markdown
## Server runbook

See `docs/navigation-server-runbook.md` for server setup, dry-run, and full execution commands.
```

- [ ] **Step 3: Run docs grep for out-of-scope fix step**

Run:

```bash
grep -R "run_fix.sh" README.md docs/navigation-server-runbook.md docs/superpowers/specs/2026-06-14-navigation-workflow-agent-design.md
```

Expected: every match says `run_fix.sh` is out of scope or not run.

- [ ] **Step 4: Run all tests**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run if git is available:

```bash
git add README.md docs/navigation-server-runbook.md
git commit -m "docs: add navigation server runbook"
```

## Task 9: Final Verification

**Files:**
- All implementation files

- [ ] **Step 1: Run unit and dry-run tests**

Run:

```bash
source .venv/bin/activate
pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Verify deterministic dry-run for `20270515`**

Run:

```bash
VLA_VLADATASETS_ROOT="$(pwd)/tests/fixtures/navigation/VLADatasets" vla-nav-agent plan --date 20270515 --dry-run --no-llm
```

Expected:

- output contains `"dataset_profile": "u_legacy_like"`
- output contains `"tool_name": "run_initial_annotation_gui"`
- output does not contain `run_fix`

- [ ] **Step 3: Verify deterministic dry-run for `20270605`**

Run:

```bash
VLA_VLADATASETS_ROOT="$(pwd)/tests/fixtures/navigation/VLADatasets" vla-nav-agent plan --date 20270605 --dry-run --no-llm
```

Expected:

- output contains `"dataset_profile": "go2w_like"`
- output contains `"tool_name": "generate_gridmap_from_pcd"`
- output contains `"human_blocking": true`
- output does not contain `run_fix`

- [ ] **Step 4: Verify SDK imports**

Run:

```bash
python - <<'PY'
from vla_data_juicer_agents.navigation.agents import create_plan_agent, create_executor_agent
print(create_plan_agent().name)
print(create_executor_agent(dry_run=True).name)
PY
```

Expected:

```text
Navigation ReAct Plan-Agent
Navigation ReAct Executor-Agent
```

- [ ] **Step 5: Final git status**

Run:

```bash
git status --short
```

Expected: clean working tree if git is initialized. If the project is not yet a git repository, record that status in the final handoff.

## Self-Review Checklist

- Spec coverage: this plan covers WSL setup, OpenAI Agents SDK agents, read-only tools, execution tools, default all-segment behavior, explicit segment behavior, `20270515`, `20270605`, `pcd_to_grid.py`, `gen_box.py`, `prepare.sh`, `run_U.sh`, `run_odom.sh`, run logs, dry-run tests, and excludes `run_fix.sh`.
- Red-flag scan: no unresolved vague implementation instructions remain.
- Type consistency: shared types are defined in `models.py`; later tasks import the same `NavigationRequest`, `WorkflowPlan`, `WorkflowStep`, `ToolResult`, and `NavigationSettings` names.
- LLM constraint: Plan-Agent and Executor-Agent are SDK `Agent` instances; deterministic `--no-llm` mode is only a local dry-run/debug path and is not the production planning path.

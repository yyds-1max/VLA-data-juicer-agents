# AgentScope Interrupt/Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current navigation workflow pause/resume patch with a durable AgentScope/Redis-backed agent runtime, while preserving the current WebSession frontend contract and removing the old workflow-control path.

**Architecture:** Mount AgentScope App inside the existing FastAPI app and use Redis-backed AgentScope storage/message bus as the durable execution kernel. Keep the existing `/api/sessions` and WebSocket surface as a compatibility layer that routes to real LLM-backed `MainRouterAgent` and `NavigationDataAgent`. Reuse existing navigation processing tools through thin wrappers and structured schemas; remove `pending_workflow` / `continue_workflow` and the Plan-Agent + Executor-Agent workflow-control path after the new path is working.

**Tech Stack:** Python 3.11+, FastAPI, AgentScope 2.0.1, Redis, DashScope/OpenAI-compatible LLM API, Pydantic v2, pytest, React 19, TypeScript, Vitest.

---

## Scope

This plan implements Phase 1 and Phase 2 from the spec:

- Phase 1: durable AgentScope backend kernel with compatibility frontend.
- Phase 2: removal or disabling of old workflow patching.

This plan does not implement Phase 3:

- No full frontend migration to AgentScope-native `/sessions`, `/chat`, or SSE.
- The current frontend continues to use the existing compatible `/api/sessions` and WebSocket event flow.

## File Map

Create:

- `src/vla_data_juicer_agents/runtime/__init__.py`
  - Runtime package marker.
- `src/vla_data_juicer_agents/runtime/agentscope_config.py`
  - Environment parsing, runtime IDs, model config, Redis URLs, and dependency checks.
- `src/vla_data_juicer_agents/runtime/agentscope_prompts.py`
  - System prompts for `MainRouterAgent` and `NavigationDataAgent`.
- `src/vla_data_juicer_agents/runtime/agentscope_bootstrap.py`
  - Creates/updates AgentScope credentials, agent records, and sessions from environment configuration.
- `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
  - Builds AgentScope App dependencies, mounts AgentScope, exposes a small service object used by the compatibility layer.
- `src/vla_data_juicer_agents/web/agent_session.py`
  - Compatibility session manager backed by AgentScope, replacing `WebSessionManager` for web app execution.
- `src/vla_data_juicer_agents/navigation/agent_tools.py`
  - Thin AgentScope tools for navigation data inspection, structured wrappers, risk checks, and human-decision external tool.
- `src/vla_data_juicer_agents/navigation/routing.py`
  - Explicit high-confidence backend route fallback for navigation requests.
- `tests/test_agentscope_runtime_config.py`
- `tests/test_agentscope_bootstrap.py`
- `tests/test_navigation_agent_tools.py`
- `tests/test_web_agentscope_session.py`
- `tests/test_web_human_decision_api.py`
- `frontend/src/components/datapilot/HumanDecisionDialog.tsx`
- `frontend/src/components/datapilot/HumanDecisionDialog.test.tsx`

Modify:

- `pyproject.toml`
  - Add runtime dependencies required by AgentScope App and DashScope model execution.
- `src/vla_data_juicer_agents/web/app.py`
  - Mount AgentScope App and switch compatible web sessions to AgentScope-backed manager by default.
- `src/vla_data_juicer_agents/web/schemas.py`
  - Add human-decision request/response schemas.
- `src/vla_data_juicer_agents/adapters/agentscope/events.py`
  - Translate AgentScope `REQUIRE_EXTERNAL_EXECUTION` for human-decision tools into compatible frontend events.
- `src/vla_data_juicer_agents/capabilities/session/orchestrator.py`
  - Remove navigation workflow tool routing semantics and pending workflow prompt from the web path.
- `src/vla_data_juicer_agents/capabilities/session/runtime.py`
  - Remove `pending_workflow_*` state or leave it only for legacy TUI tests behind an explicit legacy path.
- `src/vla_data_juicer_agents/capabilities/session/toolkit.py`
  - Stop registering `vla_run_workflow` / `vla_continue_workflow` for the web AgentScope path.
- `src/vla_data_juicer_agents/core/tool/registry.py`
  - Stop default-loading old VLA workflow control tools for the web path; keep legacy registration only if tests still require the old TUI path.
- `src/vla_data_juicer_agents/navigation/workflow.py`
  - Stop auto-confirming AgentScope confirmation events; retire it from web navigation processing.
- `src/vla_data_juicer_agents/tools/vla/run_workflow.py`
  - Disable or mark old workflow-control tools as legacy; preserve reusable internals if needed.
- `frontend/src/api/types.ts`
  - Add human-decision event and API types.
- `frontend/src/api/client.ts`
  - Add `submitHumanDecision(...)`.
- `frontend/src/store/eventReducer.ts`
  - Track pending human-decision event.
- `frontend/src/components/datapilot/DataPilotWindow.tsx`
  - Render the generic decision dialog in the chat window.

## Implementation Rules

- Use `.venv/bin/python` for local Python checks when AgentScope imports are involved.
- Do not rewrite real navigation processing functions in `navigation/execution_tools.py`.
- New navigation tools should be thin wrappers over existing functions/scripts where possible.
- All production agents must be configured with real model credentials from environment variables.
- Unit tests may use fakes for storage, message bus, and chat service. Do not fake the production agent construction path in application code.
- Commit after each task or small group of tightly coupled test/code changes.

---

### Task 1: Add Runtime Dependencies And Configuration

**Files:**

- Modify: `pyproject.toml`
- Create: `src/vla_data_juicer_agents/runtime/__init__.py`
- Create: `src/vla_data_juicer_agents/runtime/agentscope_config.py`
- Test: `tests/test_agentscope_runtime_config.py`

- [ ] **Step 1: Write failing tests for environment config**

Create `tests/test_agentscope_runtime_config.py`:

```python
from __future__ import annotations

import pytest

from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig


def test_config_reads_required_environment(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setenv("VLA_AGENT_MODEL", "qwen-plus")
    monkeypatch.setenv("VLA_AGENT_REDIS_URL", "redis://redis:6379/2")
    monkeypatch.setenv("VLA_AGENT_USER_ID", "company-user")

    config = AgentScopeRuntimeConfig.from_env()

    assert config.user_id == "company-user"
    assert config.redis_url == "redis://redis:6379/2"
    assert config.dashscope_api_key == "sk-test"
    assert config.router_model == "qwen-plus"
    assert config.navigation_model == "qwen-plus"
    assert config.main_router_agent_id == "main-router-agent"
    assert config.navigation_agent_id == "navigation-data-agent"


def test_config_supports_separate_router_and_navigation_models(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.setenv("VLA_AGENT_MODEL", "qwen-default")
    monkeypatch.setenv("VLA_AGENT_ROUTER_MODEL", "qwen-router")
    monkeypatch.setenv("VLA_AGENT_NAVIGATION_MODEL", "qwen-navigation")

    config = AgentScopeRuntimeConfig.from_env()

    assert config.router_model == "qwen-router"
    assert config.navigation_model == "qwen-navigation"


def test_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("VLA_AGENT_MODEL", "qwen-plus")

    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        AgentScopeRuntimeConfig.from_env()


def test_config_requires_model(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test")
    monkeypatch.delenv("VLA_AGENT_MODEL", raising=False)
    monkeypatch.delenv("VLA_AGENT_ROUTER_MODEL", raising=False)
    monkeypatch.delenv("VLA_AGENT_NAVIGATION_MODEL", raising=False)

    with pytest.raises(RuntimeError, match="VLA_AGENT_MODEL"):
        AgentScopeRuntimeConfig.from_env()
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_agentscope_runtime_config.py -q
```

Expected: fail with `ModuleNotFoundError: No module named 'vla_data_juicer_agents.runtime'`.

- [ ] **Step 3: Add dependencies**

Modify `pyproject.toml` dependencies:

```toml
dependencies = [
    "agentscope==2.0.1",
    "apscheduler>=3.10",
    "fastapi>=0.115",
    "openai>=1.0",
    "pydantic>=2.7",
    "PyYAML>=6.0",
    "redis>=5.0",
    "rich>=13.7.0",
    "uvicorn[standard]>=0.30",
]
```

Rationale:

- AgentScope App imports scheduler components that require `apscheduler`.
- AgentScope Redis backends require `redis.asyncio`.
- DashScope model implementation calls the OpenAI-compatible client through `openai`.

- [ ] **Step 4: Implement runtime config**

Create `src/vla_data_juicer_agents/runtime/__init__.py`:

```python
"""Runtime integration for durable AgentScope-backed web sessions."""
```

Create `src/vla_data_juicer_agents/runtime/agentscope_config.py`:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentScopeRuntimeConfig:
    user_id: str
    redis_url: str
    workspace_root: Path
    dashscope_api_key: str
    dashscope_base_url: str | None
    default_model: str
    router_model: str
    navigation_model: str
    credential_id: str = "dashscope-env"
    main_router_agent_id: str = "main-router-agent"
    navigation_agent_id: str = "navigation-data-agent"
    agentscope_mount_path: str = "/api/agentscope"

    @classmethod
    def from_env(cls, *, workspace_root: str | Path | None = None) -> "AgentScopeRuntimeConfig":
        api_key = _required_env("DASHSCOPE_API_KEY")
        default_model = (
            _env("VLA_AGENT_MODEL")
            or _env("VLA_AGENT_ROUTER_MODEL")
            or _env("VLA_AGENT_NAVIGATION_MODEL")
        )
        if not default_model:
            raise RuntimeError(
                "VLA_AGENT_MODEL is required unless both router and navigation models are configured."
            )

        root = Path(
            workspace_root
            or _env("VLA_AGENT_WORKSPACE_ROOT")
            or _env("VLA_DATA_AGENT_WEB_WORKING_DIR")
            or "./.djx"
        ).expanduser()

        return cls(
            user_id=_env("VLA_AGENT_USER_ID") or "default",
            redis_url=_env("VLA_AGENT_REDIS_URL") or "redis://localhost:6379/0",
            workspace_root=root,
            dashscope_api_key=api_key,
            dashscope_base_url=_env("DASHSCOPE_BASE_URL"),
            default_model=default_model,
            router_model=_env("VLA_AGENT_ROUTER_MODEL") or default_model,
            navigation_model=_env("VLA_AGENT_NAVIGATION_MODEL") or default_model,
            agentscope_mount_path=_env("VLA_AGENTSCOPE_MOUNT_PATH") or "/api/agentscope",
        )


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_env(name: str) -> str:
    value = _env(name)
    if value is None:
        raise RuntimeError(f"{name} is required for AgentScope-backed agent execution.")
    return value
```

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_agentscope_runtime_config.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add pyproject.toml src/vla_data_juicer_agents/runtime/__init__.py src/vla_data_juicer_agents/runtime/agentscope_config.py tests/test_agentscope_runtime_config.py
git commit -m "Add AgentScope runtime configuration"
```

---

### Task 2: Define Agent Prompts And Bootstrap Records

**Files:**

- Create: `src/vla_data_juicer_agents/runtime/agentscope_prompts.py`
- Create: `src/vla_data_juicer_agents/runtime/agentscope_bootstrap.py`
- Test: `tests/test_agentscope_bootstrap.py`

- [ ] **Step 1: Write tests for prompt constraints and bootstrap calls**

Create `tests/test_agentscope_bootstrap.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from agentscope.app.storage import AgentRecord

from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_prompts import navigation_agent_prompt


@dataclass
class FakeStorage:
    credentials: list[tuple[str, object]] = field(default_factory=list)
    agents: list[tuple[str, AgentRecord]] = field(default_factory=list)

    async def upsert_credential(self, user_id, credential_data):
        self.credentials.append((user_id, credential_data))
        return "dashscope-env"

    async def upsert_agent(self, user_id, agent_record):
        self.agents.append((user_id, agent_record))
        return agent_record.data.id


def _config() -> AgentScopeRuntimeConfig:
    return AgentScopeRuntimeConfig(
        user_id="u1",
        redis_url="redis://localhost:6379/0",
        workspace_root=".",
        dashscope_api_key="sk-test",
        dashscope_base_url=None,
        default_model="qwen-plus",
        router_model="qwen-router",
        navigation_model="qwen-nav",
    )


@pytest.mark.asyncio
async def test_bootstrap_creates_credential_and_two_agents():
    storage = FakeStorage()

    records = await bootstrap_agentscope_records(storage, _config())

    assert records.main_router_agent_id == "main-router-agent"
    assert records.navigation_agent_id == "navigation-data-agent"
    assert len(storage.credentials) == 1
    assert [record.data.name for _, record in storage.agents] == [
        "MainRouterAgent",
        "NavigationDataAgent",
    ]
    assert all("mock" not in record.data.system_prompt.lower() for _, record in storage.agents)


def test_navigation_prompt_requires_plan_execute_react_and_human_decision():
    prompt = navigation_agent_prompt()

    assert "plan-and-execute" in prompt
    assert "ReAct" in prompt
    assert "request_human_decision" in prompt
    assert "Do not ask the user to type" in prompt
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
.venv/bin/python -m pytest tests/test_agentscope_bootstrap.py -q
```

Expected: fail because modules do not exist.

- [ ] **Step 3: Implement prompts**

Create `src/vla_data_juicer_agents/runtime/agentscope_prompts.py`:

```python
from __future__ import annotations


def main_router_prompt() -> str:
    return (
        "You are MainRouterAgent for the VLA data processing system. "
        "You must use the configured real LLM model. "
        "Decide whether the user's message is ordinary conversation or a navigation data processing request. "
        "Navigation requests include VLA navigation data, ROS bag/db3, odom, trajectory, gridmap, camera calibration, "
        "dataset extraction, sync_data, finish_data, annotation, gen_box.py, tracking, or projection. "
        "When the request is navigation-related, emit a concise routing response that the backend can use to route "
        "to NavigationDataAgent. Do not call old workflow tools such as vla_run_workflow or vla_continue_workflow. "
        "For ambiguous messages, ask one short clarifying question in the user's language."
    )


def navigation_agent_prompt() -> str:
    return (
        "You are NavigationDataAgent, a dedicated real-LLM agent for VLA navigation data processing. "
        "Use a plan-and-execute working style combined with ReAct tool use. "
        "First investigate the dataset and user request, then draft a WorkflowPlan, then execute the plan step by step. "
        "Think through each step, call tools through the SDK, and update plan/task state after meaningful progress. "
        "Before any real data processing, explain the current camera parameters in normal conversation and call "
        "request_human_decision for user approval. Do not ask the user to type a magic word such as 确认. "
        "Before overwrite or delete operations, explain the target paths and call request_human_decision. "
        "The frontend dialog has confirm, stop, and guidance text actions; read the returned decision and continue, stop, or replan. "
        "The annotation GUI may be launched directly and can block until the user finishes. "
        "Retries after failures do not require confirmation unless the retry would overwrite or delete outputs. "
        "Reuse existing processing tools; do not invent shell commands when a registered tool exists. "
        "Write progress and final summaries in the user's language."
    )
```

- [ ] **Step 4: Implement bootstrap**

Create `src/vla_data_juicer_agents/runtime/agentscope_bootstrap.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from agentscope.agent import ContextConfig, ReActConfig
from agentscope.app.storage import AgentData, AgentRecord, StorageBase
from agentscope.credential import DashScopeCredential

from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_prompts import (
    main_router_prompt,
    navigation_agent_prompt,
)


@dataclass(frozen=True)
class BootstrappedAgentRecords:
    credential_id: str
    main_router_agent_id: str
    navigation_agent_id: str


async def bootstrap_agentscope_records(
    storage: StorageBase,
    config: AgentScopeRuntimeConfig,
) -> BootstrappedAgentRecords:
    credential = DashScopeCredential(
        api_key=config.dashscope_api_key,
        **({"base_url": config.dashscope_base_url} if config.dashscope_base_url else {}),
    )
    credential_id = await storage.upsert_credential(config.user_id, credential)

    await storage.upsert_agent(
        config.user_id,
        _agent_record(
            user_id=config.user_id,
            agent_id=config.main_router_agent_id,
            name="MainRouterAgent",
            prompt=main_router_prompt(),
            max_iters=8,
        ),
    )
    await storage.upsert_agent(
        config.user_id,
        _agent_record(
            user_id=config.user_id,
            agent_id=config.navigation_agent_id,
            name="NavigationDataAgent",
            prompt=navigation_agent_prompt(),
            max_iters=40,
        ),
    )
    return BootstrappedAgentRecords(
        credential_id=credential_id,
        main_router_agent_id=config.main_router_agent_id,
        navigation_agent_id=config.navigation_agent_id,
    )


def _agent_record(
    *,
    user_id: str,
    agent_id: str,
    name: str,
    prompt: str,
    max_iters: int,
) -> AgentRecord:
    return AgentRecord(
        user_id=user_id,
        data=AgentData(
            id=agent_id,
            name=name,
            system_prompt=prompt,
            context_config=ContextConfig(tool_result_limit=6000),
            react_config=ReActConfig(max_iters=max_iters, stop_on_reject=False),
        ),
    )
```

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_agentscope_bootstrap.py tests/test_agentscope_runtime_config.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/runtime/agentscope_prompts.py src/vla_data_juicer_agents/runtime/agentscope_bootstrap.py tests/test_agentscope_bootstrap.py
git commit -m "Bootstrap real AgentScope agent records"
```

---

### Task 3: Build AgentScope Runtime Factory And Mount App

**Files:**

- Create: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Modify: `src/vla_data_juicer_agents/web/app.py`
- Test: `tests/test_web_api.py`

- [ ] **Step 1: Add tests for optional AgentScope mounting**

Append to `tests/test_web_api.py`:

```python
def test_create_app_mounts_agentscope_when_runtime_factory_provided(tmp_path: Path):
    from fastapi import FastAPI

    class FakeRuntime:
        def __init__(self):
            self.app = FastAPI()
            self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")

    fake_runtime = FakeRuntime()

    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        agentscope_runtime=fake_runtime,
    )

    mounted_paths = [route.path for route in app.routes]
    assert "/api/agentscope" in mounted_paths
    assert app.state.agentscope_runtime is fake_runtime


def test_create_app_keeps_legacy_controller_when_agentscope_runtime_missing(tmp_path: Path):
    client = make_client(tmp_path)

    session_id = _create_session(client)

    assert FakeController.created[0].started is True
    assert session_id.startswith("session_")
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_api.py::test_create_app_mounts_agentscope_when_runtime_factory_provided -q
```

Expected: fail because `create_app` does not accept `agentscope_runtime`.

- [ ] **Step 3: Implement runtime factory**

Create `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`:

```python
from __future__ import annotations

from dataclasses import dataclass

from agentscope.app import create_app as create_agentscope_app
from agentscope.app.message_bus import RedisMessageBus
from agentscope.app.storage import RedisStorage
from agentscope.app.workspace_manager import LocalWorkspaceManager
from fastapi import FastAPI

from vla_data_juicer_agents.runtime.agentscope_bootstrap import bootstrap_agentscope_records
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig


@dataclass
class AgentScopeRuntime:
    config: AgentScopeRuntimeConfig
    storage: RedisStorage
    message_bus: RedisMessageBus
    workspace_manager: LocalWorkspaceManager
    app: FastAPI


def create_agentscope_runtime(config: AgentScopeRuntimeConfig) -> AgentScopeRuntime:
    storage = RedisStorage(redis_url=config.redis_url)
    message_bus = RedisMessageBus(redis_url=config.redis_url)
    workspace_manager = LocalWorkspaceManager(root_dir=str(config.workspace_root / "agentscope-workspaces"))
    app = create_agentscope_app(
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        extra_agent_tools=None,
        title="DataPilot AgentScope Runtime",
    )

    @app.on_event("startup")
    async def _bootstrap_agents() -> None:
        await bootstrap_agentscope_records(storage, config)

    return AgentScopeRuntime(
        config=config,
        storage=storage,
        message_bus=message_bus,
        workspace_manager=workspace_manager,
        app=app,
    )
```

If AgentScope's `LocalWorkspaceManager` constructor does not accept `root_dir`, inspect `.venv/lib/python3.12/site-packages/agentscope/app/workspace_manager/_local_workspace_manager.py` and adjust only this constructor call. Add a focused test for the actual constructor if needed.

- [ ] **Step 4: Modify `create_app` signature and mount runtime**

Modify `src/vla_data_juicer_agents/web/app.py`:

```python
def create_app(
    working_dir: str | None = None,
    model: str | None = None,
    db_path: str | Path | None = None,
    controller_factory: ControllerFactory = SessionController,
    frontend_dist: str | Path | None = None,
    agentscope_runtime: Any | None = None,
) -> FastAPI:
```

After `app.state.bus = bus`, add:

```python
    app.state.agentscope_runtime = agentscope_runtime
    if agentscope_runtime is not None:
        mount_path = agentscope_runtime.config.agentscope_mount_path
        app.mount(mount_path, agentscope_runtime.app)
```

Keep legacy `WebSessionManager` behavior when `agentscope_runtime is None`. This preserves existing tests and keeps TUI development paths alive until later tasks switch the default web runtime.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_api.py -q
```

Expected: all web API tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/runtime/agentscope_runtime.py src/vla_data_juicer_agents/web/app.py tests/test_web_api.py
git commit -m "Mount AgentScope runtime in web app"
```

---

### Task 4: Add Human-Decision External Tool And Event Translation

**Files:**

- Create: `src/vla_data_juicer_agents/navigation/agent_tools.py`
- Modify: `src/vla_data_juicer_agents/adapters/agentscope/events.py`
- Test: `tests/test_navigation_agent_tools.py`
- Test: `tests/test_agentscope_event_adapter.py`

- [ ] **Step 1: Write tests for human-decision tool metadata**

Create `tests/test_navigation_agent_tools.py`:

```python
from __future__ import annotations

from agentscope.permission import PermissionBehavior

from vla_data_juicer_agents.navigation.agent_tools import HumanDecisionTool


def test_human_decision_tool_is_external_and_schema_is_stable():
    tool = HumanDecisionTool()

    assert tool.name == "request_human_decision"
    assert tool.is_external_tool is True
    assert tool.is_read_only is True
    assert set(tool.input_schema["properties"]) == {"decision_type", "request_id", "summary"}
    assert tool.input_schema["required"] == ["decision_type", "request_id", "summary"]


async def test_human_decision_tool_permission_is_allowed():
    tool = HumanDecisionTool()

    decision = await tool.check_permissions({}, None)

    assert decision.behavior == PermissionBehavior.ALLOW
```

- [ ] **Step 2: Write tests for frontend event translation**

Append to `tests/test_agentscope_event_adapter.py`:

```python
from agentscope.event import RequireExternalExecutionEvent


def test_human_decision_external_event_emits_confirmation_request():
    scope, events = _scope_and_events()
    adapter = AgentScopeEventAdapter(scope)

    adapter.accept(
        RequireExternalExecutionEvent(
            reply_id="reply-1",
            tool_calls=[
                ToolCallBlock(
                    id="decision-1",
                    name="request_human_decision",
                    input={
                        "decision_type": "camera_params",
                        "request_id": "camera-20270605",
                        "summary": "请确认相机参数。",
                    },
                )
            ],
        )
    )

    assert events == [
        {
            "type": "human_decision_required",
            "source": "plan-agent",
            "run_id": "run-1",
            "parent_run_id": None,
            "timestamp": events[0]["timestamp"],
            "payload": {
                "tool_call_id": "decision-1",
                "reply_id": "reply-1",
                "decision_type": "camera_params",
                "request_id": "camera-20270605",
                "summary": "请确认相机参数。",
            },
        }
    ]
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_navigation_agent_tools.py tests/test_agentscope_event_adapter.py::test_human_decision_external_event_emits_confirmation_request -q
```

Expected: fail because `HumanDecisionTool` and event translation do not exist.

- [ ] **Step 4: Implement `HumanDecisionTool`**

Create `src/vla_data_juicer_agents/navigation/agent_tools.py`:

```python
from __future__ import annotations

from typing import Any

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase


class HumanDecisionTool(ToolBase):
    name = "request_human_decision"
    description = (
        "Pause navigation processing and ask the user for a decision in the frontend dialog. "
        "Use this for camera parameter confirmation, overwrite confirmation, and delete confirmation. "
        "The frontend returns action=confirm, action=stop, or action=guide with text."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "decision_type": {
                "type": "string",
                "enum": ["camera_params", "overwrite", "delete", "other"],
            },
            "request_id": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["decision_type", "request_id", "summary"],
        "additionalProperties": False,
    }
    is_concurrency_safe = False
    is_read_only = True
    is_external_tool = True

    async def check_permissions(self, tool_input: dict[str, Any], context: Any) -> PermissionDecision:
        return PermissionDecision(behavior=PermissionBehavior.ALLOW)
```

- [ ] **Step 5: Extend `AgentScopeEventAdapter.accept`**

Modify `src/vla_data_juicer_agents/adapters/agentscope/events.py`.

Add branch after tool-call branches:

```python
        elif event_type == "REQUIRE_EXTERNAL_EXECUTION":
            self._handle_require_external_execution(event)
```

Add method:

```python
    def _handle_require_external_execution(self, event: object) -> None:
        reply_id = _text(getattr(event, "reply_id", ""))
        for tool_call in getattr(event, "tool_calls", []) or []:
            name = _text(getattr(tool_call, "name", ""))
            if name != "request_human_decision":
                continue
            raw_input = getattr(tool_call, "input", {}) or {}
            payload = raw_input if isinstance(raw_input, dict) else {}
            self._scope.emit(
                "human_decision_required",
                reply_id=reply_id,
                tool_call_id=_text(getattr(tool_call, "id", "")),
                decision_type=_text(payload.get("decision_type", "other")),
                request_id=_text(payload.get("request_id", "")),
                summary=_text(payload.get("summary", "")),
            )
```

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_navigation_agent_tools.py tests/test_agentscope_event_adapter.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/navigation/agent_tools.py src/vla_data_juicer_agents/adapters/agentscope/events.py tests/test_navigation_agent_tools.py tests/test_agentscope_event_adapter.py
git commit -m "Add durable human decision tool"
```

---

### Task 5: Add Navigation Tool Wrappers Without Rewriting Processing Logic

**Files:**

- Modify: `src/vla_data_juicer_agents/navigation/agent_tools.py`
- Test: `tests/test_navigation_agent_tools.py`

- [ ] **Step 1: Extend tests for tool list and wrapper reuse**

Append to `tests/test_navigation_agent_tools.py`:

```python
from vla_data_juicer_agents.navigation.agent_tools import build_navigation_agent_tools


def test_build_navigation_agent_tools_includes_human_decision_and_existing_processing_tools():
    tools = build_navigation_agent_tools(dry_run=True)
    names = {tool.name for tool in tools}

    assert "request_human_decision" in names
    assert "prepare_raw_data_tool" in names
    assert "extract_and_sync_navigation_data_tool" in names
    assert "run_initial_annotation_gui_tool" in names
    assert "run_tracking_tool" in names


def test_build_navigation_agent_tools_does_not_register_old_workflow_control_tools():
    tools = build_navigation_agent_tools(dry_run=True)
    names = {tool.name for tool in tools}

    assert "vla_run_workflow" not in names
    assert "vla_continue_workflow" not in names
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_navigation_agent_tools.py -q
```

Expected: fail because `build_navigation_agent_tools` does not exist.

- [ ] **Step 3: Implement wrapper list**

Modify `src/vla_data_juicer_agents/navigation/agent_tools.py`:

```python
from vla_data_juicer_agents.navigation.execution_tools import create_navigation_execution_tools


def build_navigation_agent_tools(*, dry_run: bool = False):
    return [
        HumanDecisionTool(),
        *create_navigation_execution_tools(dry_run=dry_run),
    ]
```

Do not edit the bodies of existing real processing functions such as `prepare_raw_data`, `extract_and_sync_navigation_data`, `run_initial_annotation_gui`, or `run_tracking` unless later tests expose an adapter-specific issue.

- [ ] **Step 4: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_navigation_agent_tools.py tests/test_navigation_execution_tools_dry_run.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/navigation/agent_tools.py tests/test_navigation_agent_tools.py
git commit -m "Reuse navigation processing tools for AgentScope agent"
```

---

### Task 6: Create AgentScope-Backed Compatible Session Manager

**Files:**

- Create: `src/vla_data_juicer_agents/web/agent_session.py`
- Modify: `src/vla_data_juicer_agents/web/schemas.py`
- Test: `tests/test_web_agentscope_session.py`

- [ ] **Step 1: Write tests for session mapping and message submission**

Create `tests/test_web_agentscope_session.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
from vla_data_juicer_agents.web.session_store import WebSessionStore


@dataclass
class FakeAgentScopeRuntime:
    config: object
    submitted: list[tuple[str, str, str, object]] = field(default_factory=list)

    async def submit_user_message(self, *, web_session_id: str, message: str):
        self.submitted.append(("submit", web_session_id, message, None))
        return "turn-agent-1"


class FakeConfig:
    user_id = "u1"
    main_router_agent_id = "main-router-agent"
    navigation_agent_id = "navigation-data-agent"


@pytest.mark.asyncio
async def test_agentscope_web_session_manager_creates_compatible_session(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = FakeAgentScopeRuntime(config=FakeConfig())
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)

    session = await manager.create_session("处理 20270605 的导航数据")

    assert session.id.startswith("session_")
    assert session.title == "处理 20270605 的导航数据"
    assert store.get_session(session.id) is not None


@pytest.mark.asyncio
async def test_agentscope_web_session_manager_submits_turn_and_persists_user_message(tmp_path: Path):
    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = FakeAgentScopeRuntime(config=FakeConfig())
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime)
    session = await manager.create_session("创建会话")

    turn_id = await manager.submit_turn(session.id, "开始处理")

    assert turn_id == "turn-agent-1"
    detail = store.get_session(session.id)
    assert detail is not None
    assert [message.content for message in detail.messages] == ["开始处理"]
    assert runtime.submitted == [("submit", session.id, "开始处理", None)]
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_agentscope_session.py -q
```

Expected: fail because `web.agent_session` does not exist.

- [ ] **Step 3: Add schemas for human-decision result**

Modify `src/vla_data_juicer_agents/web/schemas.py`:

```python
HumanDecisionAction = Literal["confirm", "stop", "guide"]


class HumanDecisionRequest(BaseModel):
    action: HumanDecisionAction
    request_id: str
    tool_call_id: str
    reply_id: str
    text: str | None = None

    @field_validator("text")
    @classmethod
    def text_required_for_guidance(cls, value: str | None, info):
        if info.data.get("action") == "guide" and not str(value or "").strip():
            raise ValueError("text is required when action is guide")
        return value


class HumanDecisionResponse(BaseModel):
    accepted: bool
```

- [ ] **Step 4: Implement compatible session manager skeleton**

Create `src/vla_data_juicer_agents/web/agent_session.py`:

```python
from __future__ import annotations

from typing import Any
from uuid import uuid4

from vla_data_juicer_agents.web.schemas import SessionRecord, generate_session_title
from vla_data_juicer_agents.web.session_store import WebSessionStore


class AgentScopeWebSessionManager:
    def __init__(self, *, store: WebSessionStore, runtime: Any) -> None:
        self._store = store
        self._runtime = runtime

    async def create_session(self, first_message: str) -> SessionRecord:
        return self._store.create_session(title=generate_session_title(first_message))

    async def submit_turn(self, session_id: str, message: str) -> str:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        self._store.append_message(session_id, role="user", content=message)
        result = await self._runtime.submit_user_message(
            web_session_id=session_id,
            message=message,
        )
        return str(result or f"turn_{uuid4().hex}")

    async def interrupt(self, session_id: str) -> bool:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        interrupt = getattr(self._runtime, "interrupt_web_session", None)
        if interrupt is None:
            return False
        return bool(await interrupt(web_session_id=session_id))
```

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_agentscope_session.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/web/agent_session.py src/vla_data_juicer_agents/web/schemas.py tests/test_web_agentscope_session.py
git commit -m "Add AgentScope-backed web session manager"
```

---

### Task 7: Implement Runtime Submission, Routing, And AgentScope Sessions

**Files:**

- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Create: `src/vla_data_juicer_agents/navigation/routing.py`
- Test: `tests/test_web_agentscope_session.py`

- [ ] **Step 1: Add routing tests**

Append to `tests/test_web_agentscope_session.py`:

```python
from vla_data_juicer_agents.navigation.routing import is_high_confidence_navigation_request


def test_navigation_rule_fallback_is_narrow_and_explicit():
    assert is_high_confidence_navigation_request("处理 20270605 的室外导航数据")
    assert is_high_confidence_navigation_request("帮我同步 rosbag db3 里的 odom 和 gridmap")
    assert not is_high_confidence_navigation_request("你好，介绍一下系统")
    assert not is_high_confidence_navigation_request("继续")
```

- [ ] **Step 2: Run and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_agentscope_session.py::test_navigation_rule_fallback_is_narrow_and_explicit -q
```

Expected: fail because `navigation.routing` does not exist.

- [ ] **Step 3: Implement high-confidence fallback routing**

Create `src/vla_data_juicer_agents/navigation/routing.py`:

```python
from __future__ import annotations

import re


_NAVIGATION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:rosbag|db3|odom|grid\s*map|gridmap|sync_data|finish_data|gen_box)\b",
        r"\b(?:trajectory|tracking|projection|annotation)\b",
        r"(?:导航数据|相机参数|标注|轨迹|栅格地图|点云|里程计)",
        r"\b20\d{6}\b.*(?:导航|ros|bag|db3|sync|tracking|projection)",
    )
]


def is_high_confidence_navigation_request(message: str) -> bool:
    text = " ".join(str(message or "").split())
    if not text:
        return False
    return any(pattern.search(text) for pattern in _NAVIGATION_PATTERNS)
```

- [ ] **Step 4: Add runtime methods in `AgentScopeRuntime`**

Modify `src/vla_data_juicer_agents/runtime/agentscope_runtime.py` to add:

```python
from uuid import uuid4

from agentscope.app.storage import ChatModelConfig, SessionConfig
from agentscope.message import UserMsg

from vla_data_juicer_agents.navigation.routing import is_high_confidence_navigation_request
```

Add fields to `AgentScopeRuntime`:

```python
    web_sessions: dict[str, tuple[str, str]]
```

When constructing, pass `web_sessions={}`.

Add methods:

```python
    async def ensure_web_session(self, web_session_id: str, *, agent_id: str, model: str) -> str:
        existing = self.web_sessions.get(web_session_id)
        if existing is not None and existing[0] == agent_id:
            return existing[1]
        session = await self.storage.upsert_session(
            self.config.user_id,
            agent_id,
            config=SessionConfig(
                workspace_id=f"workspace-{web_session_id}",
                name=web_session_id,
                chat_model_config=ChatModelConfig(
                    type="dashscope_chat",
                    credential_id=self.config.credential_id,
                    model=model,
                    parameters={"parallel_tool_calls": False},
                ),
            ),
        )
        self.web_sessions[web_session_id] = (agent_id, session.id)
        return session.id

    async def submit_user_message(self, *, web_session_id: str, message: str) -> str:
        agent_id = (
            self.config.navigation_agent_id
            if is_high_confidence_navigation_request(message)
            else self.config.main_router_agent_id
        )
        model = (
            self.config.navigation_model
            if agent_id == self.config.navigation_agent_id
            else self.config.router_model
        )
        session_id = await self.ensure_web_session(web_session_id, agent_id=agent_id, model=model)
        chat_service = self.app.state.chat_service
        await chat_service.run(
            self.config.user_id,
            session_id,
            agent_id,
            UserMsg(content=message),
        )
        return f"turn_{uuid4().hex}"
```

If `UserMsg` requires a `name` argument in this installed AgentScope version, adjust to `UserMsg(name="user", content=message)` and add a regression test using the actual constructor.

- [ ] **Step 5: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_agentscope_session.py -q
```

Expected: tests pass with fakes; full runtime method may need a small fake `chat_service` test if constructor signatures differ.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/runtime/agentscope_runtime.py src/vla_data_juicer_agents/navigation/routing.py tests/test_web_agentscope_session.py
git commit -m "Route compatible web sessions to AgentScope agents"
```

---

### Task 8: Wire Web App To AgentScope Manager By Default When Configured

**Files:**

- Modify: `src/vla_data_juicer_agents/web/app.py`
- Modify: `src/vla_data_juicer_agents/web/cli.py`
- Test: `tests/test_web_api.py`
- Test: `tests/test_web_cli.py`

- [ ] **Step 1: Add app tests for AgentScope-backed manager selection**

Append to `tests/test_web_api.py`:

```python
def test_create_app_uses_agentscope_session_manager_when_runtime_present(tmp_path: Path):
    from fastapi import FastAPI

    class FakeRuntime:
        def __init__(self):
            self.app = FastAPI()
            self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")
            self.submitted = []

        async def submit_user_message(self, *, web_session_id, message):
            self.submitted.append((web_session_id, message))
            return "turn-agent-1"

    runtime = FakeRuntime()
    app = create_app(
        working_dir=str(tmp_path / ".djx"),
        db_path=tmp_path / "sessions.sqlite",
        controller_factory=FakeController,
        agentscope_runtime=runtime,
    )
    client = TestClient(app)
    session_id = _create_session(client)

    response = client.post(f"/api/sessions/{session_id}/turns", json={"message": "开始处理"})

    assert response.status_code == 200
    assert response.json()["turn_id"] == "turn-agent-1"
    assert FakeController.created == []
    assert runtime.submitted == [(session_id, "开始处理")]
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_api.py::test_create_app_uses_agentscope_session_manager_when_runtime_present -q
```

Expected: fail because `create_app` still always uses legacy manager.

- [ ] **Step 3: Modify `web/app.py` manager selection**

In `create_app`, import:

```python
from vla_data_juicer_agents.web.agent_session import AgentScopeWebSessionManager
```

Replace manager creation with:

```python
    if agentscope_runtime is None:
        manager = WebSessionManager(
            store=store,
            working_dir=working_dir,
            model=model,
            controller_factory=controller_factory,
        )
    else:
        manager = AgentScopeWebSessionManager(store=store, runtime=agentscope_runtime)
```

Update route handlers because manager methods may be async:

```python
        session = manager.create_session(request.message)
        if inspect.isawaitable(session):
            session = await session
```

Use the same pattern for `submit_turn` and `interrupt`, or add helper:

```python
async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
```

Then:

```python
        turn_id = await _maybe_await(manager.submit_turn(session_id, request.message))
```

Skip `_drain_controller_events(...)` when `agentscope_runtime is not None`; AgentScope event bridge will be added in later tasks.

- [ ] **Step 4: Wire CLI runtime creation**

Modify `src/vla_data_juicer_agents/web/cli.py`:

- Add env flag `VLA_AGENT_ENABLE_AGENTSCOPE`, default `"1"` for server mode.
- If enabled, build `AgentScopeRuntimeConfig.from_env(workspace_root=working_dir)` and `create_agentscope_runtime(config)`.
- Pass `agentscope_runtime=runtime` into `create_app`.
- If config raises because LLM env vars are missing, print a clear error and exit non-zero in server mode.

Use this skeleton:

```python
from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig
from vla_data_juicer_agents.runtime.agentscope_runtime import create_agentscope_runtime


def _agentscope_runtime_from_env(working_dir: str):
    if os.environ.get("VLA_AGENT_ENABLE_AGENTSCOPE", "1").strip() in {"0", "false", "False"}:
        return None
    config = AgentScopeRuntimeConfig.from_env(workspace_root=working_dir)
    return create_agentscope_runtime(config)
```

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_api.py tests/test_web_cli.py -q
```

Expected: all tests pass. Existing tests that do not configure AgentScope still use `FakeController`.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/web/app.py src/vla_data_juicer_agents/web/cli.py tests/test_web_api.py tests/test_web_cli.py
git commit -m "Use AgentScope session manager in web app"
```

---

### Task 9: Bridge AgentScope Events To Existing WebSocket Bus

**Files:**

- Modify: `src/vla_data_juicer_agents/web/agent_session.py`
- Modify: `src/vla_data_juicer_agents/web/app.py`
- Test: `tests/test_web_agentscope_session.py`

- [ ] **Step 1: Add fake event subscription test**

Append to `tests/test_web_agentscope_session.py`:

```python
@pytest.mark.asyncio
async def test_agentscope_web_session_manager_forwards_agent_events(tmp_path: Path):
    events = []

    class RuntimeWithEvents(FakeAgentScopeRuntime):
        async def subscribe_web_session_events(self, *, web_session_id):
            yield {
                "type": "assistant_delta",
                "source": "NavigationDataAgent",
                "payload": {"delta": "处理中"},
            }

    store = WebSessionStore(tmp_path / "sessions.sqlite")
    runtime = RuntimeWithEvents(config=FakeConfig())
    manager = AgentScopeWebSessionManager(store=store, runtime=runtime, event_callback=events.append)
    session = await manager.create_session("创建会话")

    await manager.forward_events_until_idle(session.id)

    assert events == [
        {
            "type": "assistant_delta",
            "source": "NavigationDataAgent",
            "payload": {"delta": "处理中"},
        }
    ]
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_agentscope_session.py::test_agentscope_web_session_manager_forwards_agent_events -q
```

Expected: fail because event forwarding is not implemented.

- [ ] **Step 3: Implement forwarding hook**

Modify `AgentScopeWebSessionManager.__init__`:

```python
    def __init__(self, *, store: WebSessionStore, runtime: Any, event_callback=None) -> None:
        self._store = store
        self._runtime = runtime
        self._event_callback = event_callback
```

Add:

```python
    async def forward_events_until_idle(self, session_id: str) -> None:
        subscribe = getattr(self._runtime, "subscribe_web_session_events", None)
        if subscribe is None:
            return
        async for event in subscribe(web_session_id=session_id):
            if self._event_callback is not None:
                self._event_callback(event)
            if event.get("type") == "final":
                payload = event.get("payload", {})
                text = payload.get("text") if isinstance(payload, dict) else None
                if isinstance(text, str) and text:
                    self._store.append_message(session_id, role="assistant", content=text)
```

- [ ] **Step 4: Update web app to start event bridge**

When constructing `AgentScopeWebSessionManager` in `web/app.py`, pass:

```python
event_callback=lambda event: asyncio.create_task(bus.publish(event["session_id"], event))
```

If event payload does not include `session_id`, wrap the callback in `agent_session.py` so `forward_events_until_idle(session_id)` adds it before publishing. Prefer keeping public WebSocket event shape unchanged, so do not leak internal AgentScope session IDs in payload.

In `submit_turn`, if `agentscope_runtime is not None`, schedule:

```python
asyncio.create_task(manager.forward_events_until_idle(session_id))
```

- [ ] **Step 5: Implement AgentScope runtime subscription**

In `AgentScopeRuntime`, add:

```python
    async def subscribe_web_session_events(self, *, web_session_id: str):
        mapped = self.web_sessions.get(web_session_id)
        if mapped is None:
            return
        _agent_id, agentscope_session_id = mapped
        async for event in self.message_bus.session_subscribe_events(agentscope_session_id):
            yield event.model_dump(mode="json") if hasattr(event, "model_dump") else event
```

Pass each raw event through `AgentScopeEventAdapter` before emitting to the compatibility bus. Create an `EventEmitter` with a callback that yields translated events. If this is awkward as an async generator, implement a small queue bridge:

```python
queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
scope = EventEmitter(CallbackEventSink(queue.put_nowait)).scope("agentscope", run_id=agentscope_session_id)
adapter = AgentScopeEventAdapter(scope)
adapter.accept(raw_event)
while not queue.empty():
    yield await queue.get()
```

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_agentscope_session.py tests/test_web_api.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/web/agent_session.py src/vla_data_juicer_agents/web/app.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py tests/test_web_agentscope_session.py
git commit -m "Bridge AgentScope events to compatible web sessions"
```

---

### Task 10: Resume Human Decisions Through ExternalExecutionResultEvent

**Files:**

- Modify: `src/vla_data_juicer_agents/web/agent_session.py`
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Modify: `src/vla_data_juicer_agents/web/app.py`
- Modify: `src/vla_data_juicer_agents/web/schemas.py`
- Test: `tests/test_web_human_decision_api.py`

- [ ] **Step 1: Write API tests for decision submission**

Create `tests/test_web_human_decision_api.py`:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from vla_data_juicer_agents.web.app import create_app
from tests.test_web_api import FakeController


class FakeDecisionRuntime:
    def __init__(self):
        self.app = FastAPI()
        self.config = SimpleNamespace(agentscope_mount_path="/api/agentscope")
        self.decisions = []

    async def submit_user_message(self, *, web_session_id, message):
        return "turn-agent-1"

    async def submit_human_decision(self, *, web_session_id, decision):
        self.decisions.append((web_session_id, decision))
        return True


def test_submit_human_decision_accepts_confirm(tmp_path: Path):
    runtime = FakeDecisionRuntime()
    client = TestClient(
        create_app(
            working_dir=str(tmp_path / ".djx"),
            db_path=tmp_path / "sessions.sqlite",
            controller_factory=FakeController,
            agentscope_runtime=runtime,
        )
    )
    session_id = client.post("/api/sessions", json={"message": "处理导航数据"}).json()["session"]["id"]

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions",
        json={
            "action": "confirm",
            "request_id": "camera-1",
            "tool_call_id": "tool-1",
            "reply_id": "reply-1",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    assert runtime.decisions[0][0] == session_id
    assert runtime.decisions[0][1]["action"] == "confirm"


def test_submit_human_decision_requires_text_for_guidance(tmp_path: Path):
    runtime = FakeDecisionRuntime()
    client = TestClient(
        create_app(
            working_dir=str(tmp_path / ".djx"),
            db_path=tmp_path / "sessions.sqlite",
            controller_factory=FakeController,
            agentscope_runtime=runtime,
        )
    )
    session_id = client.post("/api/sessions", json={"message": "处理导航数据"}).json()["session"]["id"]

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions",
        json={
            "action": "guide",
            "request_id": "camera-1",
            "tool_call_id": "tool-1",
            "reply_id": "reply-1",
            "text": "",
        },
    )

    assert response.status_code == 422
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_human_decision_api.py -q
```

Expected: fail because endpoint does not exist or schema validation is incomplete.

- [ ] **Step 3: Add `submit_human_decision` to web manager**

Modify `src/vla_data_juicer_agents/web/agent_session.py`:

```python
    async def submit_human_decision(self, session_id: str, decision: dict[str, Any]) -> bool:
        if self._store.get_session(session_id) is None:
            raise KeyError(session_id)
        submit = getattr(self._runtime, "submit_human_decision", None)
        if submit is None:
            return False
        return bool(await submit(web_session_id=session_id, decision=decision))
```

- [ ] **Step 4: Implement runtime resume event**

Modify `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`:

```python
import json

from agentscope.event import ExternalExecutionResultEvent
from agentscope.message import ToolResultBlock, ToolResultState
```

Add:

```python
    async def submit_human_decision(self, *, web_session_id: str, decision: dict[str, Any]) -> bool:
        mapped = self.web_sessions.get(web_session_id)
        if mapped is None:
            return False
        agent_id, agentscope_session_id = mapped
        tool_call_id = str(decision["tool_call_id"])
        reply_id = str(decision["reply_id"])
        result = ToolResultBlock(
            id=tool_call_id,
            name="request_human_decision",
            output=json.dumps(
                {
                    "action": decision["action"],
                    "text": decision.get("text"),
                    "request_id": decision["request_id"],
                },
                ensure_ascii=False,
            ),
            state=ToolResultState.SUCCESS,
        )
        await self.app.state.chat_service.run(
            self.config.user_id,
            agentscope_session_id,
            agent_id,
            ExternalExecutionResultEvent(
                reply_id=reply_id,
                execution_results=[result],
            ),
        )
        return True
```

If `ToolResultBlock` field names differ in AgentScope 2.0.1, inspect `.venv/lib/python3.12/site-packages/agentscope/message/_base.py` and update this block plus tests together.

- [ ] **Step 5: Add FastAPI endpoint**

Modify `src/vla_data_juicer_agents/web/app.py`:

```python
from vla_data_juicer_agents.web.schemas import HumanDecisionRequest, HumanDecisionResponse
```

Add route:

```python
    @app.post("/api/sessions/{session_id}/human-decisions", response_model=HumanDecisionResponse)
    async def submit_human_decision(session_id: str, request: HumanDecisionRequest) -> HumanDecisionResponse:
        submit = getattr(manager, "submit_human_decision", None)
        if submit is None:
            raise HTTPException(status_code=409, detail="Human decisions are not supported by this session runtime.")
        try:
            accepted = await _maybe_await(submit(session_id, request.model_dump(mode="json")))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        if not accepted:
            raise HTTPException(status_code=409, detail="No pending human decision is available for this session.")
        return HumanDecisionResponse(accepted=True)
```

- [ ] **Step 6: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_web_human_decision_api.py tests/test_web_api.py tests/test_web_agentscope_session.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/web/agent_session.py src/vla_data_juicer_agents/runtime/agentscope_runtime.py src/vla_data_juicer_agents/web/app.py src/vla_data_juicer_agents/web/schemas.py tests/test_web_human_decision_api.py
git commit -m "Resume human decisions through AgentScope"
```

---

### Task 11: Remove Old Web Workflow-Control Routing

**Files:**

- Modify: `src/vla_data_juicer_agents/capabilities/session/orchestrator.py`
- Modify: `src/vla_data_juicer_agents/capabilities/session/runtime.py`
- Modify: `src/vla_data_juicer_agents/capabilities/session/toolkit.py`
- Modify: `src/vla_data_juicer_agents/core/tool/registry.py`
- Test: `tests/test_session_tool_registry.py`
- Test: `tests/test_web_session_manager.py`

- [ ] **Step 1: Add tests proving old workflow tools are not in web toolkit**

Modify `tests/test_session_tool_registry.py`:

```python
from vla_data_juicer_agents.capabilities.session.toolkit import get_session_tool_specs


def test_web_session_toolkit_excludes_old_vla_workflow_control_tools():
    names = {spec.name for spec in get_session_tool_specs()}

    assert "vla_run_workflow" not in names
    assert "vla_continue_workflow" not in names
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_session_tool_registry.py::test_web_session_toolkit_excludes_old_vla_workflow_control_tools -q
```

Expected: fail because default registry still loads old workflow tools.

- [ ] **Step 3: Stop default-loading legacy workflow tools for session toolkit**

Modify `src/vla_data_juicer_agents/core/tool/registry.py`:

```python
def _ensure_default_tools() -> None:
    global _DEFAULTS_LOADED
    if _DEFAULTS_LOADED:
        return
    _DEFAULTS_LOADED = True
```

If TUI tests still need `vla_run_workflow`, add a separate explicit function:

```python
def register_legacy_vla_workflow_tools() -> None:
    from vla_data_juicer_agents.tools.vla.run_workflow import VLA_CONTINUE_WORKFLOW, VLA_RUN_WORKFLOW

    for spec in (VLA_RUN_WORKFLOW, VLA_CONTINUE_WORKFLOW):
        if spec.name not in _REGISTRY:
            register_tool_spec(spec)
```

Only call this function from legacy CLI/TUI paths that still need the old behavior. Do not call it from the web AgentScope path.

- [ ] **Step 4: Remove pending workflow fields from active web runtime**

Modify `src/vla_data_juicer_agents/capabilities/session/runtime.py`:

Remove from `SessionState`:

```python
    pending_workflow_run_dir: str | None = None
    pending_workflow_status: str | None = None
    pending_workflow_input_type: str | None = None
```

Remove pending workflow serialization in `context_payload`.

If existing TUI tests require these fields, keep a separate `LegacySessionState` only in the legacy module and update tests to assert web runtime no longer exposes `pending_workflow`.

- [ ] **Step 5: Simplify old orchestrator prompt**

Modify `src/vla_data_juicer_agents/capabilities/session/orchestrator.py`:

- Remove instructions telling the agent to call `vla_run_workflow`.
- Remove instructions telling the agent to call `vla_continue_workflow`.
- Add a short legacy warning if this class is still used outside web:

```python
"Navigation data processing is handled by the dedicated web NavigationDataAgent. "
"Do not call legacy workflow-control tools from this session agent."
```

- [ ] **Step 6: Run tests and update expectations**

Run:

```bash
.venv/bin/python -m pytest tests/test_session_tool_registry.py tests/test_web_session_manager.py tests/test_tui_integration.py -q
```

Expected: tests pass after updating only tests that asserted old `pending_workflow` behavior. Do not update tests to preserve the old web workflow-control path.

- [ ] **Step 7: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/core/tool/registry.py src/vla_data_juicer_agents/capabilities/session/runtime.py src/vla_data_juicer_agents/capabilities/session/toolkit.py src/vla_data_juicer_agents/capabilities/session/orchestrator.py tests/test_session_tool_registry.py tests/test_web_session_manager.py tests/test_tui_integration.py
git commit -m "Remove legacy workflow tools from web session path"
```

---

### Task 12: Retire Plan-Agent / Executor-Agent Web Workflow Path

**Files:**

- Modify: `src/vla_data_juicer_agents/navigation/workflow.py`
- Modify: `src/vla_data_juicer_agents/tools/vla/run_workflow.py`
- Test: `tests/test_agentscope_event_adapter.py`
- Test: `tests/test_navigation_workflow_models.py`

- [ ] **Step 1: Replace auto-confirmation test expectation**

Find and update tests that assert auto-confirmation in `tests/test_agentscope_event_adapter.py`.

Replace `test_agent_lifecycle_is_emitted_once_across_confirmation_rounds` with:

```python
def test_run_agent_stream_does_not_auto_confirm_user_confirmation_events():
    scope, events = _scope_and_events()

    class ConfirmingAgent:
        async def reply_stream(self, _message):
            yield RequireUserConfirmEvent(
                reply_id="reply-1",
                tool_calls=[ToolCallBlock(id="call-1", name="inspect", input={})],
            )

    with pytest.raises(RuntimeError, match="requires user confirmation"):
        asyncio.run(_run_agent_stream(ConfirmingAgent(), "prompt", event_scope=scope))
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_agentscope_event_adapter.py::test_run_agent_stream_does_not_auto_confirm_user_confirmation_events -q
```

Expected: fail because `_run_agent_stream` currently auto-confirms.

- [ ] **Step 3: Stop auto-confirming in `_run_agent_stream`**

Modify `src/vla_data_juicer_agents/navigation/workflow.py`:

- Remove imports of `ConfirmResult` and `UserConfirmResultEvent` if they are only used for auto-confirmation.
- In the `RequireUserConfirmEvent` handling branch, raise an explicit exception instead of fabricating confirmation:

```python
            if event_type == "REQUIRE_USER_CONFIRM":
                adapter.close_active_tools("failed")
                raise RuntimeError(
                    "AgentScope tool call requires user confirmation; web navigation must use durable human-decision events."
                )
```

This makes accidental use of the old executor path fail loudly instead of silently confirming.

- [ ] **Step 4: Disable old `continue_vla_workflow` path**

Modify `src/vla_data_juicer_agents/tools/vla/run_workflow.py`:

- Keep models importable for historical artifacts if tests use them.
- Change `continue_vla_workflow(...)` to return a structured disabled result:

```python
return RunVLAWorkflowOutput(
    ok=False,
    status="disabled",
    error_type="legacy_workflow_resume_disabled",
    message="Legacy VLA workflow resume is disabled. Use the AgentScope NavigationDataAgent session instead.",
).model_dump(mode="json")
```

- Change `VLA_CONTINUE_WORKFLOW.description` to say it is legacy-disabled.
- If `run_vla_workflow(...)` is still needed by legacy CLI tests, keep it but remove all `_set_pending_workflow(...)` calls and make calibration pause return `status="disabled"` or `status="failed"` with the same `legacy_workflow_resume_disabled` error. The web path must not use this tool.

- [ ] **Step 5: Run tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_agentscope_event_adapter.py tests/test_navigation_workflow_models.py tests/test_session_tool_registry.py -q
```

Expected: tests pass after expectations are updated away from auto-confirmation and pending workflow resume.

- [ ] **Step 6: Commit**

Run:

```bash
git add src/vla_data_juicer_agents/navigation/workflow.py src/vla_data_juicer_agents/tools/vla/run_workflow.py tests/test_agentscope_event_adapter.py tests/test_navigation_workflow_models.py tests/test_session_tool_registry.py
git commit -m "Disable legacy navigation workflow resume path"
```

---

### Task 13: Add Frontend Human-Decision Dialog

**Files:**

- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/store/eventReducer.ts`
- Create: `frontend/src/components/datapilot/HumanDecisionDialog.tsx`
- Create: `frontend/src/components/datapilot/HumanDecisionDialog.test.tsx`
- Modify: `frontend/src/components/datapilot/DataPilotWindow.tsx`
- Test: `frontend/src/store/eventReducer.test.ts`
- Test: `frontend/src/api/client.test.ts`

- [ ] **Step 1: Add reducer tests**

Modify `frontend/src/store/eventReducer.test.ts`:

```ts
import { applyAgentEvent, createEmptyRunState } from "./eventReducer";

it("tracks pending human decision events", () => {
  const state = createEmptyRunState();

  applyAgentEvent(state, {
    type: "human_decision_required",
    source: "NavigationDataAgent",
    payload: {
      reply_id: "reply-1",
      tool_call_id: "tool-1",
      request_id: "camera-1",
      decision_type: "camera_params",
      summary: "请确认相机参数。",
    },
  });

  expect(state.pendingHumanDecision).toEqual({
    replyId: "reply-1",
    toolCallId: "tool-1",
    requestId: "camera-1",
    decisionType: "camera_params",
    summary: "请确认相机参数。",
  });
});
```

- [ ] **Step 2: Add client test**

Modify `frontend/src/api/client.test.ts`:

```ts
import { submitHumanDecision } from "./client";

it("submits human decision result", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ accepted: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );

  await expect(
    submitHumanDecision("session-1", {
      action: "guide",
      request_id: "camera-1",
      tool_call_id: "tool-1",
      reply_id: "reply-1",
      text: "请改用另一组参数",
    }),
  ).resolves.toBe(true);

  expect(fetchMock).toHaveBeenCalledWith(
    "/api/sessions/session-1/human-decisions",
    expect.objectContaining({ method: "POST" }),
  );
});
```

- [ ] **Step 3: Run frontend tests and verify failure**

Run:

```bash
cd frontend
npm test -- --run src/store/eventReducer.test.ts src/api/client.test.ts
```

Expected: fail because types and functions do not exist.

- [ ] **Step 4: Add frontend types and client**

Modify `frontend/src/api/types.ts`:

```ts
export type HumanDecisionAction = "confirm" | "stop" | "guide";

export interface PendingHumanDecision {
  replyId: string;
  toolCallId: string;
  requestId: string;
  decisionType: string;
  summary: string;
}

export interface HumanDecisionPayload {
  action: HumanDecisionAction;
  request_id: string;
  tool_call_id: string;
  reply_id: string;
  text?: string;
}
```

Modify `frontend/src/api/client.ts`:

```ts
import type { HumanDecisionPayload } from "./types";

export async function submitHumanDecision(
  sessionId: string,
  payload: HumanDecisionPayload,
): Promise<boolean> {
  const data = await requestJson<{ accepted: boolean }>(
    `${sessionPath(sessionId)}/human-decisions`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  return data.accepted;
}
```

- [ ] **Step 5: Extend reducer state**

Modify `frontend/src/store/eventReducer.ts`:

```ts
import type { AgentEvent, PendingHumanDecision } from "../api/types";
```

Add to `RunState`:

```ts
pendingHumanDecision: PendingHumanDecision | null;
```

Add to `createEmptyRunState()`:

```ts
pendingHumanDecision: null,
```

Add branch in `applyAgentEvent` before fallback system event:

```ts
  if (type === "human_decision_required") {
    state.pendingHumanDecision = {
      replyId: normalizeText(payload.reply_id),
      toolCallId: normalizeText(payload.tool_call_id),
      requestId: normalizeText(payload.request_id),
      decisionType: normalizeText(payload.decision_type) || "other",
      summary: normalizeText(payload.summary),
    };
    state.running = false;
    state.activeText = "";
    state.activeStartedAt = null;
    return;
  }
```

Clear `pendingHumanDecision` when the user submits a decision in the UI store action, not on arbitrary assistant deltas.

- [ ] **Step 6: Implement dialog component**

Create `frontend/src/components/datapilot/HumanDecisionDialog.tsx`:

```tsx
import { useState } from "react";
import type { PendingHumanDecision } from "../../api/types";

interface HumanDecisionDialogProps {
  decision: PendingHumanDecision | null;
  onConfirm: () => Promise<void> | void;
  onStop: () => Promise<void> | void;
  onGuide: (text: string) => Promise<void> | void;
}

export function HumanDecisionDialog({
  decision,
  onConfirm,
  onStop,
  onGuide,
}: HumanDecisionDialogProps) {
  const [text, setText] = useState("");
  if (!decision) {
    return null;
  }

  return (
    <div role="dialog" aria-modal="true" aria-label="需要确认" className="datapilot-decision-dialog">
      <p>{decision.summary || "请确认是否继续。"}</p>
      <div>
        <button type="button" onClick={() => void onConfirm()}>确认</button>
        <button type="button" onClick={() => void onStop()}>停止</button>
      </div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          const guidance = text.trim();
          if (guidance) {
            void onGuide(guidance);
            setText("");
          }
        }}
      >
        <input
          aria-label="引导文本"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="输入引导文本"
        />
        <button type="submit" disabled={!text.trim()}>发送</button>
      </form>
    </div>
  );
}
```

Style it using existing DataPilot classes or nearby component conventions. Keep it in the chat window, not a global route-level page.

- [ ] **Step 7: Wire dialog into `DataPilotWindow.tsx`**

In the component that owns current `sessionId` and run state:

- Render `HumanDecisionDialog`.
- For confirm:

```ts
await submitHumanDecision(sessionId, {
  action: "confirm",
  request_id: decision.requestId,
  tool_call_id: decision.toolCallId,
  reply_id: decision.replyId,
});
```

- For stop use `action: "stop"`.
- For guidance use `action: "guide"` and `text`.
- Clear `pendingHumanDecision` in local/store state after successful submit.

- [ ] **Step 8: Run frontend tests**

Run:

```bash
cd frontend
npm test -- --run src/store/eventReducer.test.ts src/api/client.test.ts src/components/datapilot/HumanDecisionDialog.test.tsx
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

Run:

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/store/eventReducer.ts frontend/src/components/datapilot/HumanDecisionDialog.tsx frontend/src/components/datapilot/HumanDecisionDialog.test.tsx frontend/src/components/datapilot/DataPilotWindow.tsx frontend/src/store/eventReducer.test.ts frontend/src/api/client.test.ts
git commit -m "Add human decision dialog to compatible frontend"
```

---

### Task 14: End-To-End Backend Verification For Durable Wait/Resume

**Files:**

- Test: `tests/test_web_human_decision_api.py`
- Test: `tests/test_web_agentscope_session.py`

- [ ] **Step 1: Add regression test for structured decision result**

Append to `tests/test_web_human_decision_api.py` using a fake runtime that captures the structured decision:

```python
def test_guidance_decision_preserves_user_text(tmp_path: Path):
    runtime = FakeDecisionRuntime()
    client = TestClient(
        create_app(
            working_dir=str(tmp_path / ".djx"),
            db_path=tmp_path / "sessions.sqlite",
            controller_factory=FakeController,
            agentscope_runtime=runtime,
        )
    )
    session_id = client.post("/api/sessions", json={"message": "处理导航数据"}).json()["session"]["id"]

    response = client.post(
        f"/api/sessions/{session_id}/human-decisions",
        json={
            "action": "guide",
            "request_id": "camera-1",
            "tool_call_id": "tool-1",
            "reply_id": "reply-1",
            "text": "请改用另一组外参",
        },
    )

    assert response.status_code == 200
    assert runtime.decisions[0][1] == {
        "action": "guide",
        "request_id": "camera-1",
        "tool_call_id": "tool-1",
        "reply_id": "reply-1",
        "text": "请改用另一组外参",
    }
```

- [ ] **Step 2: Add runtime-level unit test for `ExternalExecutionResultEvent`**

Use a fake `chat_service` to capture the event in `tests/test_web_agentscope_session.py`:

```python
@pytest.mark.asyncio
async def test_runtime_submits_external_execution_result_for_human_decision():
    captured = {}

    class ChatService:
        async def run(self, user_id, session_id, agent_id, input_msg):
            captured["user_id"] = user_id
            captured["session_id"] = session_id
            captured["agent_id"] = agent_id
            captured["input_msg"] = input_msg

    runtime = make_runtime_with_fake_app_state(ChatService())
    runtime.web_sessions["web-1"] = ("navigation-data-agent", "as-session-1")

    accepted = await runtime.submit_human_decision(
        web_session_id="web-1",
        decision={
            "action": "guide",
            "request_id": "camera-1",
            "tool_call_id": "tool-1",
            "reply_id": "reply-1",
            "text": "请改用另一组外参",
        },
    )

    assert accepted is True
    assert captured["session_id"] == "as-session-1"
    assert captured["agent_id"] == "navigation-data-agent"
    assert captured["input_msg"].type == "EXTERNAL_EXECUTION_RESULT"
```

Implement `make_runtime_with_fake_app_state(...)` locally in the test with `SimpleNamespace`.

- [ ] **Step 3: Run backend focused suite**

Run:

```bash
.venv/bin/python -m pytest tests/test_agentscope_runtime_config.py tests/test_agentscope_bootstrap.py tests/test_navigation_agent_tools.py tests/test_web_agentscope_session.py tests/test_web_human_decision_api.py tests/test_agentscope_event_adapter.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

Run:

```bash
git add tests/test_web_human_decision_api.py tests/test_web_agentscope_session.py
git commit -m "Verify durable human decision resume flow"
```

---

### Task 15: Full Regression And Cleanup

**Files:**

- Modify only files needed to fix failures found by this task.

- [ ] **Step 1: Run full backend test suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass.

If failures are in legacy tests that explicitly assert `pending_workflow` or `vla_continue_workflow`, update those tests to assert the new Phase 2 behavior:

- Old resume path is disabled.
- Web navigation runs through AgentScope-backed session manager.
- Human decision waits resume through `ExternalExecutionResultEvent`.

- [ ] **Step 2: Run frontend tests**

Run:

```bash
cd frontend
npm test
```

Expected: all frontend unit tests pass.

- [ ] **Step 3: Run frontend build**

Run:

```bash
cd frontend
npm run build
```

Expected: TypeScript and Vite build pass.

- [ ] **Step 4: Run import smoke test**

Run:

```bash
.venv/bin/python -c "from vla_data_juicer_agents.web.app import create_app; from vla_data_juicer_agents.runtime.agentscope_config import AgentScopeRuntimeConfig; print('ok')"
```

Expected:

```text
ok
```

- [ ] **Step 5: Manual server smoke test with Redis**

With Redis running and real model env vars set:

```bash
export DASHSCOPE_API_KEY='real-key'
export VLA_AGENT_MODEL='qwen-plus'
export VLA_AGENT_REDIS_URL='redis://localhost:6379/0'
.venv/bin/python -m vla_data_juicer_agents.web.cli --working-dir ./.djx
```

Manual checks:

- Create a web session from the current frontend.
- Send a clear navigation request.
- Confirm the event stream shows `NavigationDataAgent` activity.
- Confirm camera parameter context appears in the conversation.
- Confirm the generic dialog appears with confirm, stop, and guidance text.
- Confirm guidance text resumes the same waiting agent session.
- Restart the server while a human decision is pending and verify Redis-backed state can surface/resume the pending decision.

- [ ] **Step 6: Remove temporary flags if not needed**

Search:

```bash
rg -n "pending_workflow|continue_vla_workflow|vla_continue_workflow|auto-confirm|auto confirm|calibration_params_not_confirmed" src tests
```

Expected:

- No active web path references `pending_workflow`.
- No active web path calls `vla_continue_workflow`.
- Any remaining references are legacy disabled tests, historical model names, or explicit error constants.

- [ ] **Step 7: Commit final cleanup**

Run:

```bash
git status --short
git add src tests frontend pyproject.toml
git commit -m "Complete AgentScope interrupt resume migration"
```

---

## Self-Review Checklist

- [ ] Phase 1 is covered by Tasks 1-10 and Task 13.
- [ ] Phase 2 is covered by Tasks 11-12 and Task 15 Step 6.
- [ ] Phase 3 is not included.
- [ ] The plan reuses existing navigation processing tools instead of rewriting them.
- [ ] Human decisions use generic frontend dialog actions: confirm, stop, guide.
- [ ] Guidance text reaches the agent as structured tool result data.
- [ ] All production agents are configured with real LLM credentials from environment variables.
- [ ] Redis is a formal runtime dependency.
- [ ] Existing `/api/sessions` and WebSocket compatibility are preserved.
- [ ] The old workflow-control path is removed or disabled, not merely bypassed.

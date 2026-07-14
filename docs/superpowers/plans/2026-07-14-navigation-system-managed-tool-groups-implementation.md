# Navigation System-Managed Tool Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make NavigationDataAgent switch between investigation/planning and plan-bound execution tools from durable navigation state inside one AgentScope reply, then switch back after a Plan completes, without giving the model generic tools or control over tool-group authorization.

**Architecture:** Introduce a navigation-owned grouped catalog and immutable surface policy, then project that policy into AgentScope `ToolGroup` objects through a NavigationDataAgent-only middleware. The middleware refreshes from the session-authorized SQLite snapshot before every reasoning iteration and after every terminal domain-tool response; the existing flat resolver becomes a compatibility adapter for direct CLI flows. `ChatService` keeps its normal Agent assembly, MainRouterAgent keeps its handoff tool, and no Plan step runs until the model explicitly calls its plan-bound execution tool.

**Tech Stack:** Python 3.12, AgentScope 2.0.1, Pydantic 2, SQLite, FastAPI, pytest.

## Global Constraints

- Change only this repository. Do not modify, fork, monkey-patch, or vendor AgentScope.
- SQLite navigation task, Plan, observation, evidence, and ledger state remains authoritative. AgentScope `activated_groups` is a cache that the middleware overwrites.
- The NavigationDataAgent model chooses investigations, stage, complete JSON Plan, steps, variants, parameters, and whether to continue after extract/sync. Code only builds tools, validates, persists, authorizes, and executes an explicitly called tool.
- Plan acceptance changes tool visibility only. It must never invoke `prepare_raw_data`, another processing action, or a workflow runner.
- Plan completion is bidirectional: the next same-reply reasoning iteration returns to the investigation/planning surface. Do not leave execution tools exposed indefinitely.
- Keep `navigation_diagnostics` as an empty, always-present extension group. Do not add diagnostic tools, diagnostic metrics, or a diagnostics-only state.
- A nonterminal/pre-claim execution error may leave the ledger-authorized action available. Do not redesign an underlying action durably finalized as failed into an automatically rerunnable step.
- NavigationDataAgent must not receive AgentScope Bash, Read/Write/Edit, Task, Schedule, Team, skill, MCP, or model-visible `reset_tools` schemas. MainRouterAgent remains unchanged.
- Keep the existing direct CLI/model-flow adapter working by flattening the active navigation surface. Do not maintain a second list of tools or phase rules.
- Preserve the current bounded response and context-compression settings. Do not change `ContextConfig(tool_result_limit=6000)` or AgentScope compression.
- Use TDD. Each task starts with a failing focused test, ends with focused verification, receives spec and code-quality review, and then commits only its own files.
- The design authority is `docs/superpowers/specs/2026-07-14-navigation-system-managed-tool-groups-design.md` at commit `c9baee2`.

## File and Responsibility Map

| Path | Responsibility after implementation |
| --- | --- |
| `src/vla_data_juicer_agents/navigation/tool_groups.py` | Navigation group names, immutable group/surface value objects, exact classification checks, and activity-to-surface policy. No AgentScope middleware or SQLite reads. |
| `src/vla_data_juicer_agents/navigation/agent_tools.py` | Build one authorized grouped tool catalog from `NavigationExecutionSnapshot`; expose grouped resolver plus flat direct adapter. |
| `src/vla_data_juicer_agents/runtime/navigation_tool_surface.py` | NavigationDataAgent-only AgentScope middleware; synchronize Toolkit in place, suppress/reject `reset_tools`, and fail closed. |
| `src/vla_data_juicer_agents/runtime/agentscope_runtime.py` | Bind Web/AgentScope session identity and cancellation to the middleware factory; keep router handoff tools; pass both factories to `create_app`. |
| `src/vla_data_juicer_agents/runtime/agentscope_prompts.py` | Compact behavioral contract for same-reply Plan execution and the reverse phase boundary. |
| `tests/test_navigation_tool_groups.py` | Pure group classification and surface-policy tests. |
| `tests/test_navigation_agent_tools.py` | Real tool-builder grouping, activity projection, flat-adapter, router-isolation, and runtime-factory tests. |
| `tests/test_navigation_tool_surface_middleware.py` | Middleware unit tests for refresh, in-place mutation, `reset_tools`, post-tool transition, retry boundary, and fail-closed behavior. |
| `tests/navigation_chat_service_harness.py` | Minimal storage/workspace/bus fixtures that run AgentScope 2.0.1's real `ChatService`, `Agent`, `Toolkit`, and ReAct loop with `ScriptedChatModel`. |
| `tests/test_navigation_chat_service_tool_groups.py` | Same-reply forward/reverse transitions, failed submission, restart/stale cache, and later same-session finish-Plan regression. |
| `tests/test_navigation_context_budget.py` | Prompt contract and exact planning/execution/recovery schema-budget regression. |

---

### Task 1: Define the Navigation Tool-Group Contract and Surface Policy

**Files:**
- Create: `src/vla_data_juicer_agents/navigation/tool_groups.py`
- Create: `tests/test_navigation_tool_groups.py`

**Interfaces:**
- Consumes: `agentscope.tool.ToolBase`; activity values `"planning"`, `"execution"`, and `"recovery_required"` already emitted by `NavigationExecutionSnapshot.activity`.
- Produces: `NavigationToolGroupDefinition`, `NavigationToolSurface`, `NavigationToolSurfacePolicy.resolve`, `classify_fixed_navigation_tools`, `NAVIGATION_GROUP_NAMES`, and fixed group-name constants used by Tasks 2–6.

- [ ] **Step 1: Write failing value-object, classification, and activity-policy tests**

Create `tests/test_navigation_tool_groups.py` with small named `FunctionTool` fixtures and these assertions:

```python
import pytest
from agentscope.tool import FunctionTool

from vla_data_juicer_agents.navigation.tool_groups import (
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_DIAGNOSTICS,
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_PLAN_AUTHORING,
    NavigationToolGroupDefinition,
    NavigationToolSurfacePolicy,
    classify_fixed_navigation_tools,
)


def _tool(name: str):
    def implementation() -> dict[str, bool]:
        return {"ok": True}
    return FunctionTool(implementation, name=name, is_read_only=True)


def test_fixed_navigation_tools_are_classified_exactly_once():
    tools = [
        _tool("list_observation_evidence_tool"),
        _tool("read_observation_evidence_tool"),
        _tool("inspect_navigation_raw_metadata_tool"),
        _tool("inspect_navigation_sensor_candidates_tool"),
        _tool("inspect_navigation_topic_candidates_tool"),
        _tool("inspect_navigation_runtime_assets_tool"),
        _tool("inspect_navigation_calibration_inventory_tool"),
        _tool("inspect_navigation_localization_sources_tool"),
        _tool("inspect_navigation_artifact_state_tool"),
        _tool("inspect_navigation_gridmap_artifacts_tool"),
        _tool("get_navigation_task_context_tool"),
        _tool("describe_processing_action_tool"),
        _tool("record_navigation_user_guidance_tool"),
        _tool("submit_extract_sync_plan_tool"),
        _tool("submit_finish_processing_plan_tool"),
        _tool("get_plan_execution_overview_tool"),
        _tool("get_current_plan_step_tool"),
    ]

    grouped = classify_fixed_navigation_tools(tools)

    assert {name: {tool.name for tool in group} for name, group in grouped.items()} == {
        NAVIGATION_EVIDENCE_READ: {
            "list_observation_evidence_tool", "read_observation_evidence_tool",
        },
        NAVIGATION_INVESTIGATION: {
            "inspect_navigation_raw_metadata_tool",
            "inspect_navigation_sensor_candidates_tool",
            "inspect_navigation_topic_candidates_tool",
            "inspect_navigation_runtime_assets_tool",
            "inspect_navigation_calibration_inventory_tool",
            "inspect_navigation_localization_sources_tool",
        },
        NAVIGATION_ARTIFACT_CHECKS: {
            "inspect_navigation_artifact_state_tool",
            "inspect_navigation_gridmap_artifacts_tool",
        },
        NAVIGATION_PLAN_AUTHORING: {
            "get_navigation_task_context_tool",
            "describe_processing_action_tool",
            "record_navigation_user_guidance_tool",
            "submit_extract_sync_plan_tool",
            "submit_finish_processing_plan_tool",
        },
        NAVIGATION_EXECUTION_STATE: {
            "get_plan_execution_overview_tool", "get_current_plan_step_tool",
        },
    }


@pytest.mark.parametrize(
    "names, message",
    [
        (["unknown_navigation_tool"], "unclassified"),
        (["read_observation_evidence_tool", "read_observation_evidence_tool"], "duplicate"),
    ],
)
def test_fixed_classification_rejects_unknown_or_duplicate_tools(names, message):
    with pytest.raises(ValueError, match=message):
        classify_fixed_navigation_tools([_tool(name) for name in names])


def test_policy_exposes_exact_groups_for_each_activity():
    all_groups = {
        name: NavigationToolGroupDefinition(name=name, description=name, tools=())
        for name in (
            NAVIGATION_EVIDENCE_READ,
            NAVIGATION_INVESTIGATION,
            NAVIGATION_ARTIFACT_CHECKS,
            NAVIGATION_PLAN_AUTHORING,
            NAVIGATION_EXECUTION_STATE,
            NAVIGATION_EXECUTION_ACTIONS,
            NAVIGATION_DIAGNOSTICS,
        )
    }

    assert NavigationToolSurfacePolicy.resolve("planning", all_groups).active_group_names == (
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_INVESTIGATION,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_PLAN_AUTHORING,
        NAVIGATION_DIAGNOSTICS,
    )
    assert NavigationToolSurfacePolicy.resolve("execution", all_groups).active_group_names == (
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_EXECUTION_STATE,
        NAVIGATION_EXECUTION_ACTIONS,
        NAVIGATION_DIAGNOSTICS,
    )
    assert NavigationToolSurfacePolicy.resolve("recovery_required", all_groups).active_group_names == (
        NAVIGATION_EVIDENCE_READ,
        NAVIGATION_ARTIFACT_CHECKS,
        NAVIGATION_EXECUTION_STATE,
        NAVIGATION_DIAGNOSTICS,
    )
    assert NavigationToolSurfacePolicy.resolve("planning", all_groups).group(NAVIGATION_DIAGNOSTICS).tools == ()
```

Also assert `NavigationToolSurface.flatten_active_tools()` returns tools in group order, rejects duplicate tool names across groups, and every returned definition has `instructions is None` so group metadata does not inject another large playbook.

- [ ] **Step 2: Run the focused tests and confirm missing-module failure**

Run: `.venv/bin/pytest tests/test_navigation_tool_groups.py -q`

Expected: collection fails with `ModuleNotFoundError: ...navigation.tool_groups`.

- [ ] **Step 3: Implement immutable group definitions and strict fixed-name classification**

Create `tool_groups.py` with this public shape and the exact name map from Step 1:

```python
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from agentscope.tool import ToolBase

NavigationActivity = Literal["planning", "execution", "recovery_required"]

NAVIGATION_EVIDENCE_READ = "navigation_evidence_read"
NAVIGATION_INVESTIGATION = "navigation_investigation"
NAVIGATION_ARTIFACT_CHECKS = "navigation_artifact_checks"
NAVIGATION_PLAN_AUTHORING = "navigation_plan_authoring"
NAVIGATION_EXECUTION_STATE = "navigation_execution_state"
NAVIGATION_EXECUTION_ACTIONS = "navigation_execution_actions"
NAVIGATION_DIAGNOSTICS = "navigation_diagnostics"
NAVIGATION_GROUP_NAMES = (
    NAVIGATION_EVIDENCE_READ,
    NAVIGATION_INVESTIGATION,
    NAVIGATION_ARTIFACT_CHECKS,
    NAVIGATION_PLAN_AUTHORING,
    NAVIGATION_EXECUTION_STATE,
    NAVIGATION_EXECUTION_ACTIONS,
    NAVIGATION_DIAGNOSTICS,
)


@dataclass(frozen=True)
class NavigationToolGroupDefinition:
    name: str
    description: str
    tools: tuple[ToolBase, ...]
    instructions: str | None = None


@dataclass(frozen=True)
class NavigationToolSurface:
    activity: NavigationActivity
    groups: tuple[NavigationToolGroupDefinition, ...]
    active_group_names: tuple[str, ...]

    def group(self, name: str) -> NavigationToolGroupDefinition:
        for group in self.groups:
            if group.name == name:
                return group
        raise LookupError(name)

    def flatten_active_tools(self) -> list[ToolBase]:
        tools = [tool for group in self.groups for tool in group.tools]
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("duplicate navigation tool names across active groups")
        return tools
```

`classify_fixed_navigation_tools(tools: Sequence[ToolBase]) -> dict[str, tuple[ToolBase, ...]]` must reject a repeated name before classification and reject every name absent from the fixed mapping. Define `NavigationToolSurfacePolicy.resolve(activity: NavigationActivity, groups_by_name: Mapping[str, NavigationToolGroupDefinition]) -> NavigationToolSurface`; it must reject a missing required definition, select only the exact ordered group tuple for that activity, and set `active_group_names` equal to the returned definition order. Use short descriptions such as `"Read bounded task evidence."`; do not put workflow instructions in group metadata.

- [ ] **Step 4: Verify the pure policy and commit**

Run: `.venv/bin/pytest tests/test_navigation_tool_groups.py -q`

Expected: all tests pass.

Commit:

```bash
git add src/vla_data_juicer_agents/navigation/tool_groups.py tests/test_navigation_tool_groups.py
git commit -m "feat: define navigation tool group policy"
```

---

### Task 2: Build One Grouped Catalog and Preserve the Flat Direct Adapter

**Files:**
- Modify: `src/vla_data_juicer_agents/navigation/agent_tools.py`
- Modify: `tests/test_navigation_agent_tools.py`

**Interfaces:**
- Consumes: Task 1's `NavigationToolGroupDefinition`, `NavigationToolSurface`, `NavigationToolSurfacePolicy`, fixed-name classifier and constants; existing `NavigationServices`, `NavigationExecutionSnapshot`, cancellation context, observation/guidance/submission/execution tool builders.
- Produces: `resolve_navigation_tool_surface(...) -> NavigationToolSurface | None`; preserved `resolve_navigation_agent_tools(...) -> list[ToolBase]` implemented only as a flattening adapter.

- [ ] **Step 1: Add failing grouped-resolver tests for planning, execution, recovery, and reverse transition**

Extend `tests/test_navigation_agent_tools.py` with a helper:

```python
def _surface(services, session_id):
    return resolve_navigation_tool_surface(
        services=services,
        agentscope_session_id=session_id,
        web_session_id=session_id,
        cancellation=None,
    )


def _group_tool_names(surface):
    return {
        group.name: {tool.name for tool in group.tools}
        for group in surface.groups
    }
```

Add tests that create the existing complete-plan fixtures and assert:

```python
planning = _surface(services_without_active_plan, session_id)
assert planning.activity == "planning"
assert "describe_processing_action_tool" in {
    tool.name for tool in planning.group(NAVIGATION_PLAN_AUTHORING).tools
}
with pytest.raises(LookupError):
    planning.group(NAVIGATION_EXECUTION_ACTIONS)

execution = _surface(services_with_active_plan, session_id)
assert execution.activity == "execution"
assert {tool.name for tool in execution.group(NAVIGATION_EXECUTION_STATE).tools} == {
    "get_plan_execution_overview_tool", "get_current_plan_step_tool",
}
assert "prepare_raw_data_tool" in {
    tool.name for tool in execution.group(NAVIGATION_EXECUTION_ACTIONS).tools
}
assert all(group.name != NAVIGATION_PLAN_AUTHORING for group in execution.groups)

recovery = _surface(services_with_recovery_handoff, session_id)
assert recovery.activity == "recovery_required"
assert all(group.name != NAVIGATION_EXECUTION_ACTIONS for group in recovery.groups)
```

Use `pytest.raises(LookupError)` for every absent-group assertion; `NavigationToolSurface.group()` has the explicit bounded failure defined in Task 1.

Add a reverse-transition test that terminalizes every step of an extract-sync Plan through the existing repository helpers, then asserts a fresh resolver call returns `activity == "planning"`, includes artifact checks and Plan authoring, and contains no execution state/action group. Add a finish-Plan completion counterpart asserting the task is completed and no execution actions remain.

Finally assert with the same bound service/session inputs:

```python
surface = _surface(services, session_id)
flat = resolve_navigation_agent_tools(
    services=services,
    agentscope_session_id=session_id,
    web_session_id=session_id,
    cancellation=None,
)
assert [tool.name for tool in flat] == [
    tool.name for tool in surface.flatten_active_tools()
]
```

- [ ] **Step 2: Run the resolver tests and confirm the grouped API is absent**

Run: `.venv/bin/pytest tests/test_navigation_agent_tools.py -q -k 'surface or grouped or reverse_transition or flat_adapter'`

Expected: import or assertion failure because only the current flat resolver exists.

- [ ] **Step 3: Refactor `agent_tools.py` around one snapshot and one grouped catalog**

Add:

```python
def resolve_navigation_tool_surface(
    *,
    services: NavigationServices,
    agentscope_session_id: str,
    cancellation: CancellationContext | None,
    web_session_id: str | None = None,
) -> NavigationToolSurface | None:
    if web_session_id is None:
        return None
    snapshot = services.plan_store.read_execution_snapshot(
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    )
    if snapshot is None:
        return None
    groups = build_navigation_tool_groups(
        services=services,
        snapshot=snapshot,
        cancellation=cancellation,
        web_session_id=web_session_id,
        agentscope_session_id=agentscope_session_id,
    )
    return NavigationToolSurfacePolicy.resolve(snapshot.activity, groups)
```

Define the builder with this exact signature:

```python
def build_navigation_tool_groups(
    *,
    services: NavigationServices,
    snapshot: NavigationExecutionSnapshot,
    cancellation: CancellationContext | None,
    web_session_id: str,
    agentscope_session_id: str,
) -> dict[str, NavigationToolGroupDefinition]:
```

Its body must:

1. build observation tools once and classify evidence/investigation/artifact/authoring observation tools;
2. add the guidance tool and both submission tools to Plan authoring only when `activity == "planning"`;
3. build compact execution-state tools and plan-bound execution actions only for an active Plan in execution/recovery;
4. return an empty diagnostics definition in every catalog;
5. pass every domain tool through existing `_trust` exactly once;
6. never inspect products itself and never execute a processing action.

Because policy requires only activity-relevant definitions, do not build planning submission schemas during execution and do not build execution wrappers during planning. Construct the fixed-name classifier from the tools that exist for the current activity, and require the exact expected subset for that activity. Keep dynamic execution-action names out of the fixed map; place all tools returned by `build_plan_bound_execution_tools(...)` in `navigation_execution_actions` after verifying their names do not collide with any fixed tool.

Replace the flat resolver body with the complete adapter:

```python
def resolve_navigation_agent_tools(
    *,
    services: NavigationServices,
    agentscope_session_id: str,
    cancellation: CancellationContext | None,
    web_session_id: str | None = None,
) -> list[ToolBase]:
    surface = resolve_navigation_tool_surface(
        services=services,
        agentscope_session_id=agentscope_session_id,
        cancellation=cancellation,
        web_session_id=web_session_id,
    )
    return [] if surface is None else surface.flatten_active_tools()
```

No caller may independently concatenate observation, guidance, submission, state, or execution lists after this task.

- [ ] **Step 4: Run grouped, Plan-submission, and execution authorization tests**

Run:

```bash
.venv/bin/pytest tests/test_navigation_agent_tools.py tests/test_navigation_plan_submission_tools.py tests/test_navigation_plan_execution.py -q
```

Expected: pass, including stale-revision submission staying in planning and existing plan-bound authorization tests.

- [ ] **Step 5: Commit the grouped resolver**

```bash
git add src/vla_data_juicer_agents/navigation/agent_tools.py tests/test_navigation_agent_tools.py
git commit -m "refactor: resolve grouped navigation tool surfaces"
```

---

### Task 3: Add the System-Controlled AgentScope Middleware

**Files:**
- Create: `src/vla_data_juicer_agents/runtime/navigation_tool_surface.py`
- Create: `tests/test_navigation_tool_surface_middleware.py`
- Modify: `tests/navigation_agentscope_harness.py`

**Interfaces:**
- Consumes: Task 2's `resolve_navigation_tool_surface(...)`; AgentScope 2.0.1 `MiddlewareBase`, `ToolGroup`, `ToolResponse`, `TextBlock`, and `ToolResultState`; an already assembled `Agent` with Toolkit and persisted `AgentState`.
- Produces: `NavigationToolSurfaceMiddleware`, `NavigationToolSurfaceSyncError`, and `build_agent_with_middlewares(...)` test helper that accepts middlewares without manual `refresh_tools()`.

- [ ] **Step 1: Write failing middleware tests with real AgentScope Toolkit state**

Create `tests/test_navigation_tool_surface_middleware.py`. Use a sequence resolver returning planning then execution surfaces and an `Agent` whose initial Toolkit contains fake generic tools named `bash`, `read`, `write`, `task`, `schedule`, `team`, and `skill_viewer`; seed `agent.state.tool_context.activated_groups` with stale execution names.

Cover these exact cases:

```python
@pytest.mark.asyncio
async def test_reasoning_refresh_overwrites_stale_groups_and_removes_generic_tools():
    group_list = agent.toolkit.tool_groups
    events = [event async for event in middleware.on_reasoning(
        agent, {"tool_choice": None}, forwarding_reasoning,
    )]
    assert agent.toolkit.tool_groups is group_list
    assert agent.toolkit.tool_groups[0].name == "basic"
    assert agent.toolkit.tool_groups[0].tools == []
    assert agent.state.tool_context.activated_groups == list(planning.active_group_names)
    assert resolver.calls == 1
    assert events == ["forwarded"]


@pytest.mark.asyncio
async def test_terminal_tool_response_refreshes_before_it_is_yielded():
    seen = []
    async for item in middleware.on_acting(
        agent,
        {"tool_call": ToolCallBlock(id="submit", name="submit_extract_sync_plan_tool", input="{}")},
        forwarding_tool,
    ):
        if isinstance(item, ToolResponse):
            seen.append(tuple(agent.state.tool_context.activated_groups))
    assert seen == [execution.active_group_names]


@pytest.mark.asyncio
async def test_model_call_hides_reset_tools_schema():
    await middleware.on_model_call(
        agent,
        {"messages": [], "tools": [reset_schema, domain_schema], "tool_choice": None, "current_model": model},
        capture_model_call,
    )
    assert captured_names == ["domain_tool"]


@pytest.mark.asyncio
async def test_fabricated_reset_tools_is_rejected_without_state_change():
    before = list(agent.state.tool_context.activated_groups)
    responses = [item async for item in middleware.on_acting(
        agent,
        {"tool_call": ToolCallBlock(id="reset", name="reset_tools", input="{}")},
        must_not_run,
    )]
    assert responses[-1].state is ToolResultState.ERROR
    assert responses[-1].metadata == {"ok": False, "error_type": "navigation_tool_groups_system_managed"}
    assert agent.state.tool_context.activated_groups == before
```

Also test that a resolver exception and a `None` surface both clear to exactly `[ToolGroup(name="basic")]`, clear activated groups, and raise `NavigationToolSurfaceSyncError("navigation tool surface unavailable")` without the original exception text. Test that a normal error-shaped `ToolResponse` still refreshes from SQLite: if the resolver still returns execution, the action remains visible; if it returns planning after a terminal failed/inactivated Plan, execution actions disappear. This tests ledger authority, not response-text parsing.

Call `on_reasoning` twice in one unit test with two different resolver results and assert the resolver call count is exactly two. This prevents an implementation from caching a surface for the lifetime of the Agent or middleware.

- [ ] **Step 2: Run the middleware tests and confirm missing implementation**

Run: `.venv/bin/pytest tests/test_navigation_tool_surface_middleware.py -q`

Expected: collection fails because `runtime.navigation_tool_surface` does not exist.

- [ ] **Step 3: Implement in-place synchronization and fail-closed clearing**

Implement the middleware constructor with explicit bound dependencies:

```python
class NavigationToolSurfaceSyncError(RuntimeError):
    """Raised after a navigation surface refresh has failed closed."""


class NavigationToolSurfaceMiddleware(MiddlewareBase):
    def __init__(
        self,
        *,
        services: NavigationServices,
        web_session_id: str,
        agentscope_session_id: str,
        cancellation: CancellationContext | None,
    ) -> None:
        self._services = services
        self._web_session_id = web_session_id
        self._agentscope_session_id = agentscope_session_id
        self._cancellation = cancellation
```

Use these internal methods:

```python
def _clear(self, agent: Agent) -> None:
    agent.toolkit.tool_groups[:] = [ToolGroup(name="basic")]
    agent.state.tool_context.activated_groups[:] = []


def _synchronize(self, agent: Agent) -> None:
    try:
        surface = resolve_navigation_tool_surface(
            services=self._services,
            web_session_id=self._web_session_id,
            agentscope_session_id=self._agentscope_session_id,
            cancellation=self._cancellation,
        )
        if surface is None:
            raise LookupError("missing authorized navigation attempt")
        groups = [
            ToolGroup(
                name=definition.name,
                description=definition.description,
                instructions=definition.instructions,
                tools=list(definition.tools),
            )
            for definition in surface.groups
        ]
        agent.toolkit.tool_groups[:] = [ToolGroup(name="basic"), *groups]
        agent.state.tool_context.activated_groups[:] = list(surface.active_group_names)
    except Exception as error:
        self._clear(agent)
        raise NavigationToolSurfaceSyncError(
            "navigation tool surface unavailable"
        ) from error
```

Preserve the list object with slice assignment. Creating a new `Toolkit`, assigning a new `tool_groups` list, or mutating AgentScope's `ResetTools` internals is forbidden.

- [ ] **Step 4: Implement the three middleware hooks**

Implement:

```python
async def on_reasoning(self, agent, input_kwargs, next_handler):
    self._synchronize(agent)
    async for item in next_handler(**input_kwargs):
        yield item


async def on_model_call(self, agent, input_kwargs, next_handler):
    tools = [
        schema for schema in input_kwargs["tools"]
        if schema.get("function", {}).get("name") != "reset_tools"
    ]
    return await next_handler(**{**input_kwargs, "tools": tools})


async def on_acting(self, agent, input_kwargs, next_handler):
    tool_call = input_kwargs["tool_call"]
    if tool_call.name == "reset_tools":
        yield ToolResponse(
            id=tool_call.id,
            content=[TextBlock(text="Navigation tool groups are system managed.")],
            state=ToolResultState.ERROR,
            metadata={
                "ok": False,
                "error_type": "navigation_tool_groups_system_managed",
            },
        )
        return
    async for item in next_handler(**input_kwargs):
        if isinstance(item, ToolResponse):
            self._synchronize(agent)
        yield item
```

Do not inspect tool-result JSON or tool names to decide a transition. Every terminal response triggers a fresh authoritative projection.

- [ ] **Step 5: Extend the offline Agent harness without changing direct-flow semantics**

Add to `tests/navigation_agentscope_harness.py`:

```python
def build_agent_with_middlewares(record, model, *, tools=None, middlewares=None, state=None):
    return Agent(
        name=record.data.name,
        system_prompt=record.data.system_prompt,
        model=model,
        toolkit=Toolkit(tools=tools or []),
        context_config=record.data.context_config,
        react_config=record.data.react_config,
        middlewares=middlewares or [],
        state=state,
    )
```

Keep existing `build_agent` and `refresh_tools` for direct CLI compatibility tests; new Web regressions must not use `refresh_tools`.

- [ ] **Step 6: Verify middleware behavior and commit**

Run:

```bash
.venv/bin/pytest tests/test_navigation_tool_surface_middleware.py tests/test_navigation_agent_tools.py -q
```

Expected: pass.

Commit:

```bash
git add src/vla_data_juicer_agents/runtime/navigation_tool_surface.py tests/test_navigation_tool_surface_middleware.py tests/navigation_agentscope_harness.py
git commit -m "feat: synchronize navigation tool groups from durable state"
```

---

### Task 4: Wire the Middleware Through ChatService and Isolate NavigationDataAgent

**Files:**
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_runtime.py`
- Modify: `tests/test_navigation_agent_tools.py`

**Interfaces:**
- Consumes: Task 3's `NavigationToolSurfaceMiddleware`; existing `_web_session_id_from_agentscope_session`, `AgentScopeRuntime._navigation_services()`, and `run_cancellation()`.
- Produces: `build_extra_agent_middlewares_factory(config, runtime=runtime)`; `create_agentscope_runtime()` passes both `extra_agent_middlewares` and `extra_agent_tools` to AgentScope.

- [ ] **Step 1: Write failing runtime-factory and router-isolation tests**

Extend `tests/test_navigation_agent_tools.py`:

```python
def test_navigation_extra_tools_are_empty_but_router_handoff_is_unchanged(tmp_path):
    config = _config(tmp_path)
    runtime = FakeNavigationHandoffRuntime()
    tools = build_extra_agent_tools_factory(config, runtime=runtime)
    assert asyncio.run(tools("alice", config.navigation_agent_id, "web-1__navigation-data-agent")) == []
    assert {tool.name for tool in asyncio.run(
        tools("alice", config.main_router_agent_id, "web-1__main-router-agent")
    )} == {"start_navigation_data_task"}


def test_navigation_middleware_factory_binds_exact_session_and_runtime(tmp_path):
    config = _config(tmp_path)
    runtime = _runtime_with_services_and_cancellation(config)
    factory = build_extra_agent_middlewares_factory(config, runtime=runtime)
    middlewares = asyncio.run(factory(
        "alice", config.navigation_agent_id, "web-1__navigation-data-agent",
    ))
    assert len(middlewares) == 1
    assert isinstance(middlewares[0], NavigationToolSurfaceMiddleware)
    assert asyncio.run(factory(
        "alice", config.main_router_agent_id, "web-1__main-router-agent",
    )) == []
```

Update the existing `fake_create_app` capture test to assert `captured["extra_agent_middlewares"]` is callable, NavigationDataAgent gets one middleware and no extra tools, and MainRouterAgent gets no middleware and its handoff tool.

- [ ] **Step 2: Run the wiring tests and confirm the factory is missing**

Run: `.venv/bin/pytest tests/test_navigation_agent_tools.py -q -k 'extra_tools or middleware_factory or create_agentscope_runtime'`

Expected: import/assertion failure because the middleware factory is not wired and navigation tools still come from `extra_agent_tools`.

- [ ] **Step 3: Make `extra_agent_tools` NavigationDataAgent-empty and add the middleware factory**

Change `build_extra_agent_tools_factory` so its NavigationDataAgent branch returns `[]` unconditionally. Preserve the MainRouterAgent branch byte-for-byte except imports required by the refactor.

Add:

```python
def build_extra_agent_middlewares_factory(
    config: AgentScopeRuntimeConfig,
    *,
    runtime: AgentScopeRuntime | None = None,
):
    async def extra_agent_middlewares(_user_id: str, agent_id: str, session_id: str):
        if agent_id != config.navigation_agent_id:
            return []
        if runtime is None:
            raise RuntimeError("navigation runtime is unavailable")
        web_session_id = _web_session_id_from_agentscope_session(
            session_id,
            agent_id=config.navigation_agent_id,
        )
        return [
            NavigationToolSurfaceMiddleware(
                services=runtime._navigation_services(),
                web_session_id=web_session_id,
                agentscope_session_id=session_id,
                cancellation=runtime.run_cancellation(session_id),
            )
        ]
    return extra_agent_middlewares
```

The runtime holder is populated before any request. Do not silently create an unbound middleware when it is absent.

- [ ] **Step 4: Pass both factories to `agentscope.app.create_app`**

Define holder-backed async wrappers for both factories and call:

```python
app = agentscope.app.create_app(
    storage=storage,
    message_bus=message_bus,
    workspace_manager=workspace_manager,
    extra_agent_middlewares=extra_agent_middlewares,
    extra_agent_tools=extra_agent_tools,
    title="DataPilot AgentScope Runtime",
)
```

Keep `AgentScopeRuntime._navigation_tools_for_session()` as the direct flat-adapter entry used by non-Web tests/flows. Do not call it from ChatService assembly.

- [ ] **Step 5: Verify runtime wiring and commit**

Run:

```bash
.venv/bin/pytest tests/test_navigation_agent_tools.py tests/test_web_agentscope_session.py -q -k 'factory or handoff or navigation_agent or router'
```

Expected: pass; existing routing and session mapping tests remain unchanged.

Commit:

```bash
git add src/vla_data_juicer_agents/runtime/agentscope_runtime.py tests/test_navigation_agent_tools.py
git commit -m "feat: wire system-managed navigation tool groups"
```

---

### Task 5: Prove Forward and Reverse Transitions With Real ChatService

**Files:**
- Create: `tests/navigation_chat_service_harness.py`
- Create: `tests/test_navigation_chat_service_tool_groups.py`
- Modify: `tests/navigation_agentscope_harness.py`

**Interfaces:**
- Consumes: AgentScope 2.0.1 `agentscope.app._service._chat.ChatService`, real `Agent`, real `Toolkit`, real ReAct loop, `ScriptedChatModel`, Task 4 factories, and existing navigation service/Plan fixtures.
- Produces: regression evidence that one `ChatService._run_impl` changes schemas between adjacent model calls without another user message or manual toolkit refresh.

- [ ] **Step 1: Build the minimal real-ChatService harness and make one smoke test pass**

Create `tests/navigation_chat_service_harness.py` with:

- `ChatServiceStorage` implementing `get_agent`, `get_session`, `upsert_message`, `get_message`, and `update_session_state`; it stores a real `AgentRecord`, `SessionConfig`, `AgentState`, and messages in memory.
- `ChatServiceWorkspaceManager.get_workspace(user_id, agent_id, session_id, workspace_id)` returning a harmless test workspace object.
- `ChatServiceBus.session_run(session_id)` as an async context manager and `session_publish_event(session_id, event)` appending event dicts.
- inert scheduler/background manager objects because `get_toolkit` is monkeypatched.

Invoke the real `ChatService._run_impl`, not a project fake and not `LocalChatService.run`. In each test monkeypatch only AgentScope assembly seams:

```python
monkeypatch.setattr(chat_service_module, "get_model", async_get_scripted_model)
monkeypatch.setattr(
    chat_service_module,
    "get_toolkit",
    async_get_generic_toolkit,
)
```

`async_get_generic_toolkit` must return `Toolkit(tools=[generic_bash, generic_read, generic_task])`; the navigation middleware must remove them. Keep the real `ChatService`, `Agent`, middleware chain, model-input preparation, tool availability checks, acting loop, event publication, and state persistence.

Smoke assertion:

```python
await service._run_impl(user_id, session_id, agent_id, UserMsg(name="user", content="处理数据"))
assert storage.updated_state is not None
assert bus.events
```

Run: `.venv/bin/pytest tests/test_navigation_chat_service_tool_groups.py -q -k smoke`

Expected: pass before adding transition scenarios; this validates the harness rather than product behavior.

- [ ] **Step 2: Add the failing same-reply Plan-acceptance regression**

Arrange one planning attempt with the observation revisions/evidence required by `valid_extract_plan_payload`. Queue this `ScriptedChatModel` sequence:

1. `submit_extract_sync_plan_tool` with the complete JSON payload and current `planning_context_revision`;
2. `get_current_plan_step_tool` using the `plan_id` read from the previous tool result in model context;
3. the current plan-bound processing action with only `plan_id` and `step_id`;
4. final text.

Assertions:

```python
planning_names = schema_names(model.invocations[0].tools)
execution_names = schema_names(model.invocations[1].tools)
assert {
    "submit_extract_sync_plan_tool", "submit_finish_processing_plan_tool",
} <= planning_names
assert "prepare_raw_data_tool" not in planning_names
assert {"get_current_plan_step_tool", "prepare_raw_data_tool"} <= execution_names
assert "describe_processing_action_tool" not in execution_names
assert "submit_extract_sync_plan_tool" not in execution_names
assert "reset_tools" not in planning_names | execution_names
assert processing_spy.calls == 1
assert model.compact_event_count == 0
assert model.assert_exhausted() is None
```

Also assert the first processing-spy call occurs only after the model invocation whose response names that processing tool. There is one `_run_impl` call, one user message, and no call to `refresh_tools()`.

Run: `.venv/bin/pytest tests/test_navigation_chat_service_tool_groups.py -q -k plan_acceptance`

Expected before implementation integration is complete: the second model call lacks `prepare_raw_data_tool`; after Tasks 2–4 it passes.

- [ ] **Step 3: Add failed-submission and stale-cache reconstruction regressions**

Add two scenarios:

- stale `planning_context_revision` submission returns `ok:false`; the next model invocation still contains Plan-authoring tools and no execution actions;
- persisted `AgentState.tool_context.activated_groups` contains execution groups while SQLite has no active Plan; a new real ChatService run reconstructs planning schemas and removes generic/reset schemas.

Then repeat the cache test with SQLite containing an active Plan and stale planning groups; the first model call must expose execution schemas. This covers restart/process reconstruction without persisting a second navigation phase state.

- [ ] **Step 4: Add the same-reply reverse transition after extract/sync completion**

Use a minimal accepted extract-sync Plan whose last action is replaced by a safe counting `FunctionTool` fixture while its wrapper still finalizes through the real ledger. Queue:

1. current execution action;
2. `inspect_navigation_artifact_state_tool` after the final ledger action;
3. final text asking whether to continue.

Assert the model invocation after the final action contains artifact checks, investigation, both complete-Plan submission tools, and no execution state/action. Assert the extract-sync Plan is `completed`, the task attempt remains `active`, and no finish Plan was created by code.

- [ ] **Step 5: Add later same-session finish planning and final closure**

Start from the persisted state produced by Step 4 and call real `ChatService._run_impl` with a second user message containing finish parameters. The first invocation of this later run must be planning. Queue relevant inspection/guidance calls, `submit_finish_processing_plan_tool`, its plan-bound actions, and final text.

Assert:

- the finish submission switches the next same-reply model call to execution;
- every processing action was model-authored and called once;
- final finish validation marks the task attempt `completed`;
- the following model call contains artifact/Plan-authoring reads and no execution action;
- the combined two-stage scripted flow has `model.compact_event_count == 0`;
- no separate resume API or old Task restoration was used.

- [ ] **Step 6: Add retry-boundary assertions**

In a real Agent/middleware test, call the current execution wrapper once with a wrong `step_id`; assert the error is nonterminal and the same SQLite-authorized action appears in the next invocation. Separately arrange a ledger step finalized `failed`; assert a fresh surface contains no executable wrapper capable of rerunning the underlying action and the processing spy remains at one call.

- [ ] **Step 7: Run the complete ChatService regression and commit**

Run:

```bash
.venv/bin/pytest tests/test_navigation_chat_service_tool_groups.py tests/test_navigation_model_authored_flow.py -q
```

Expected: pass; existing direct-flow tests may still use flat `refresh_tools`, while every new Web/ChatService test uses automatic middleware synchronization.

Commit:

```bash
git add tests/navigation_chat_service_harness.py tests/test_navigation_chat_service_tool_groups.py tests/navigation_agentscope_harness.py
git commit -m "test: prove navigation tool transitions in ChatService"
```

---

### Task 6: Tighten the Prompt Contract and Schema Budget

**Files:**
- Modify: `src/vla_data_juicer_agents/runtime/agentscope_prompts.py`
- Modify: `tests/test_navigation_context_budget.py`
- Modify: `tests/test_agentscope_bootstrap.py`

**Interfaces:**
- Consumes: grouped resolver from Task 2 and middleware behavior from Tasks 3–5.
- Produces: a compact model contract that does not claim submission auto-executes and budget tests over actual planning/execution/recovery surfaces.

- [ ] **Step 1: Write failing prompt and exact-schema-set tests**

Add prompt assertions:

```python
prompt = navigation_agent_prompt()
assert "Plan submission never starts processing" in prompt
assert "continue the same reply" in prompt
assert "read the accepted Plan's current step" in prompt
assert "call the matching plan-bound tool" in prompt
assert "do not use generic shell or file tools" in prompt
assert "after the last Plan step" in prompt
assert "investigation/planning tools become available again" in prompt
```

Do not assert a literal inventory of every tool name in prompt text.

Add helpers that serialize `surface.flatten_active_tools()` schemas for planning, execution, and recovery. Assert:

- planning excludes execution state/actions and all generic/reset names;
- execution excludes investigation, `describe_processing_action_tool`, both submissions, generic/reset names;
- recovery excludes execution actions and Plan authoring;
- diagnostics exists but contributes zero schemas;
- each surface has unique tool names;
- execution schema characters are less than planning schema characters;
- prompt + guidance + active surface schemas + compact anchor stays below the existing static budget threshold already used by the test suite.

- [ ] **Step 2: Run prompt/budget tests and confirm the new control language is absent**

Run:

```bash
.venv/bin/pytest tests/test_navigation_context_budget.py tests/test_agentscope_bootstrap.py -q -k 'prompt or schema or budget or surface'
```

Expected: prompt assertions fail before the wording change.

- [ ] **Step 3: Add only the missing phase-transition guidance**

In `navigation_agent_prompt()` replace the single execution invariant with compact bullets equivalent to:

```text
- Plan submission never starts processing. After a complete Plan is accepted, continue the same reply, read the accepted Plan's current step, and call the matching plan-bound tool with only its Plan and step identity.
- Treat tool availability as the current system-managed phase boundary. Do not search for generic shell, file, task, skill, or MCP workarounds.
- After the last Plan step completes, investigation/planning tools become available again. Verify products and decide the next conversational action; after extract/sync, ask whether the user wants to continue before authoring finish work.
```

Keep the existing domain guidance file and its four bounded few-shots unchanged; its current stage-boundary guidance is compatible with these three bullets. Do not add group inventories, AgentScope implementation details, or diagnostics prose to model context.

- [ ] **Step 4: Verify context and bootstrap regressions**

Run:

```bash
.venv/bin/pytest tests/test_navigation_context_budget.py tests/test_agentscope_bootstrap.py tests/test_navigation_agents.py -q
```

Expected: pass; `ContextConfig(tool_result_limit=6000)` remains unchanged.

- [ ] **Step 5: Commit prompt and budget coverage**

```bash
git add src/vla_data_juicer_agents/runtime/agentscope_prompts.py tests/test_navigation_context_budget.py tests/test_agentscope_bootstrap.py
git commit -m "fix: teach navigation agent explicit plan execution"
```

---

### Task 7: Remove Obsolete Web Tool Assembly Assumptions and Run Full Verification

**Files:**
- Modify: `tests/test_navigation_model_authored_flow.py` to label manual `refresh_tools()` coverage as the direct flat-adapter path and keep it out of Web acceptance assertions.
- Modify: `tests/test_web_agentscope_session.py` to assert NavigationDataAgent uses the middleware factory instead of domain tools in `basic`.
- Modify: `tests/test_session_tool_registry.py` to remove any NavigationDataAgent expectation for generic/basic tools while preserving MainRouterAgent registry coverage.
- Verify: all files changed in Tasks 1–6

**Interfaces:**
- Consumes: completed implementation from Tasks 1–6.
- Produces: one coherent Web path with no duplicated flat phase routing, no NavigationDataAgent generic tools, and full local verification evidence.

- [ ] **Step 1: Run a dead-assumption audit**

Run:

```bash
rg -n "extra_agent_tools.*navigation|_navigation_tools_for_session\(" src tests
rg -n "Toolkit\(tools=resolve_navigation_agent_tools|refresh_tools\(" tests
```

Expected interpretation:

- production Web assembly has no NavigationDataAgent call from `extra_agent_tools` to `_navigation_tools_for_session` or the flat resolver;
- `_navigation_tools_for_session` remains only as the explicit direct flat adapter;
- `refresh_tools` appears only in direct CLI/model-flow tests, never in `test_navigation_chat_service_tool_groups.py` or Web runtime assembly.

Delete old assertions that expect NavigationDataAgent domain tools in AgentScope `basic`. Rewrite them to assert empty navigation extras plus one NavigationDataAgent middleware. Do not delete direct adapter coverage.

- [ ] **Step 2: Run focused navigation and Web suites**

Run:

```bash
.venv/bin/pytest \
  tests/test_navigation_tool_groups.py \
  tests/test_navigation_agent_tools.py \
  tests/test_navigation_tool_surface_middleware.py \
  tests/test_navigation_chat_service_tool_groups.py \
  tests/test_navigation_model_authored_flow.py \
  tests/test_navigation_context_budget.py \
  tests/test_web_agentscope_session.py \
  tests/test_session_tool_registry.py -q
```

Expected: pass.

- [ ] **Step 3: Run full Python verification and static checks**

Run:

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: all tests pass; compileall succeeds; `git diff --check` prints nothing.

- [ ] **Step 4: Inspect scope and confirm forbidden changes are absent**

Run:

```bash
git diff --stat c9baee2..HEAD
git diff --name-only c9baee2..HEAD
git status --short
```

Expected: changes are limited to the file map above and test adjustments required by them. There are no edits under `.venv`, no AgentScope vendoring, no SQLite schema/migration change, no new diagnostics implementation, and no processing action implementation change.

- [ ] **Step 5: Commit the explicit direct/Web test-boundary cleanup**

Commit exactly the three test files listed for this task:

```bash
git add tests/test_navigation_model_authored_flow.py tests/test_web_agentscope_session.py tests/test_session_tool_registry.py
git commit -m "test: remove obsolete navigation tool assembly assumptions"
```

- [ ] **Step 6: Record implementation handoff evidence**

In the implementation handoff, report the exact focused/full commands and pass counts, the commits created by Tasks 1–7, and the remaining server acceptance step. Do not run or synchronize a real server job without a separate user request.

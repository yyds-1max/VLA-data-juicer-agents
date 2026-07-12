import asyncio
from types import SimpleNamespace

import pytest

from vla_data_juicer_agents.navigation.agents import (
    EXECUTOR_AGENT_INSTRUCTIONS,
    PLAN_AGENT_INSTRUCTIONS,
    create_executor_agent,
    create_plan_agent,
)
from vla_data_juicer_agents.navigation.models import NavigationRequest
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.workflow import (
    direct_completed_entry_state,
    direct_execution_terminal_state,
    prepare_direct_navigation_entry,
    run_direct_plan_until_submitted,
    run_executor_agent,
    run_plan_agent,
)


class _SilentAgent:
    async def reply_stream(self, _message):
        if False:
            yield None


class _PlanStore:
    def __init__(self, plan=None):
        self.plan = plan
        self.calls = []

    def get_active(self, task_id, phase):
        self.calls.append((task_id, phase))
        return self.plan


def test_create_plan_agent_uses_only_resolved_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    tools = [SimpleNamespace(name="get_phase_planning_context_tool")]

    agent = create_plan_agent(tools=tools)

    assert agent.tools == tools
    assert "complete JSON plan" in agent.instructions
    assert "data_profile_draft" not in agent.instructions


def test_create_executor_agent_uses_only_plan_bound_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    tools = [SimpleNamespace(name="execute_current_plan_step_tool")]

    agent = create_executor_agent(tools=tools, dry_run=True)

    assert agent.tools == tools
    assert "plan_id and step_id" in agent.instructions
    assert "Dry-run mode" in agent.instructions


def test_run_plan_agent_loads_active_plan_from_repository(monkeypatch):
    stored = SimpleNamespace(plan_id="plan-1", task_id="task-1", phase="extract_sync")
    store = _PlanStore(stored)

    plan = asyncio.run(
        run_plan_agent(
            _SilentAgent(),
            NavigationRequest(date="20270605"),
            plan_store=store,
            task_id="task-1",
            phase="extract_sync",
        )
    )

    assert plan is stored
    assert store.calls == [("task-1", "extract_sync")]


def test_run_plan_agent_rejects_assistant_text_without_stored_plan():
    with pytest.raises(RuntimeError, match="did not submit a valid complete plan"):
        asyncio.run(
            run_plan_agent(
                _SilentAgent(),
                NavigationRequest(date="20270605"),
                plan_store=_PlanStore(),
                task_id="task-1",
                phase="extract_sync",
            )
        )


def test_direct_planning_refreshes_phase_tools_after_durable_observation_progress(
    monkeypatch,
):
    stored = SimpleNamespace(
        plan_id="plan-1",
        plan_revision=1,
        task_id="task-1",
        phase="extract_sync",
    )
    task = SimpleNamespace(
        task_id="task-1",
        phase=SimpleNamespace(value="extract_sync"),
        state_revision=1,
    )
    store = _PlanStore()
    task_store = SimpleNamespace(get_task=lambda _task_id: task)
    services = SimpleNamespace(task_store=task_store, plan_store=store)
    resolved = []
    rounds = 0

    def resolve(**_kwargs):
        resolved.append(task.state_revision)
        return [SimpleNamespace(name=f"round-{task.state_revision}")]

    class RoundAgent:
        async def reply_stream(self, _message):
            nonlocal rounds
            rounds += 1
            if rounds == 1:
                task.state_revision += 1
            else:
                store.plan = stored
            if False:
                yield None

    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.workflow.resolve_navigation_agent_tools",
        resolve,
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.workflow.create_plan_agent",
        lambda **_kwargs: RoundAgent(),
    )

    plan = asyncio.run(
        run_direct_plan_until_submitted(
            services=services,
            task=task,
            request=NavigationRequest(date="20270605"),
            agentscope_session_id="direct-session",
        )
    )

    assert plan is stored
    assert resolved == [1, 2]


def test_direct_planning_stops_after_one_round_without_durable_progress(monkeypatch):
    task = SimpleNamespace(
        task_id="task-1",
        phase=SimpleNamespace(value="extract_sync"),
        state_revision=1,
    )
    services = SimpleNamespace(
        task_store=SimpleNamespace(get_task=lambda _task_id: task),
        plan_store=_PlanStore(),
    )
    resolutions = 0

    def resolve(**_kwargs):
        nonlocal resolutions
        resolutions += 1
        return []

    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.workflow.resolve_navigation_agent_tools",
        resolve,
    )
    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.workflow.create_plan_agent",
        lambda **_kwargs: _SilentAgent(),
    )

    with pytest.raises(RuntimeError, match="did not submit a valid complete plan"):
        asyncio.run(
            run_direct_plan_until_submitted(
                services=services,
                task=task,
                request=NavigationRequest(date="20270605"),
                agentscope_session_id="direct-session",
            )
        )

    assert resolutions == 1


def test_direct_planning_uses_post_resolver_revision_as_round_boundary(tmp_path, monkeypatch):
    raw_segment = tmp_path / "data" / "raw_data" / "20270605" / "segment-1"
    raw_segment.mkdir(parents=True)
    settings = NavigationSettings(
        vladatasets_root=tmp_path / "data",
        runs_root=tmp_path / "runs",
    )
    request = NavigationRequest(date="20270605", segments=["segment-1"], dry_run=True)
    services, task = prepare_direct_navigation_entry(
        run_dir=tmp_path / "run",
        request=request,
        settings=settings,
        agentscope_session_id="direct-session",
    )
    rounds = 0

    def silent_agent(**_kwargs):
        nonlocal rounds
        rounds += 1
        return _SilentAgent()

    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.workflow.create_plan_agent",
        silent_agent,
    )

    with pytest.raises(RuntimeError, match="did not submit a valid complete plan"):
        asyncio.run(
            run_direct_plan_until_submitted(
                services=services,
                task=task,
                request=request,
                agentscope_session_id="direct-session",
            )
        )

    assert rounds == 1


@pytest.mark.parametrize(
    ("task_status", "phase", "current_step", "expected_status", "ok"),
    [
        ("failed", "extract_sync", "step-1", "failed", False),
        ("needs_replan", "extract_sync", "step-1", "needs_replan", False),
        ("waiting_user", "finish_processing", "confirm", "waiting_user", False),
        ("running", "extract_sync", "step-1", "incomplete", False),
        ("completed", "completed", None, "completed", True),
    ],
)
def test_direct_terminal_state_is_derived_from_durable_ledger(
    task_status, phase, current_step, expected_status, ok
):
    task = SimpleNamespace(
        task_id="task-1",
        status=SimpleNamespace(value=task_status),
        phase=SimpleNamespace(value=phase),
    )
    overview = SimpleNamespace(
        model_dump=lambda mode="json": {
            "plan_id": "plan-1",
            "status": "active",
            "current_step_id": current_step,
        }
    )
    services = SimpleNamespace(
        task_store=SimpleNamespace(get_task=lambda _task_id: task),
        plan_store=SimpleNamespace(
            get=lambda _plan_id: SimpleNamespace(
                plan_id="plan-1",
                status="completed" if task_status == "completed" else "active",
            ),
            get_execution_overview=lambda _plan_id: overview,
            get_current_step=lambda _plan_id: None if current_step is None else {"step": {"step_id": current_step}},
        ),
    )

    result = direct_execution_terminal_state(
        services=services,
        task_id="task-1",
        plan_id="plan-1",
    )

    assert result["status"] == expected_status
    assert result["ok"] is ok


def test_completed_entry_skips_planning_and_reports_reconciled_artifacts():
    snapshot = SimpleNamespace(
        model_dump=lambda mode="json": {"final_outputs_exist": True, "final_grid_map_exists": True}
    )
    task = SimpleNamespace(
        task_id="task-1",
        phase=SimpleNamespace(value="completed"),
        status=SimpleNamespace(value="completed"),
        artifact_snapshot=snapshot,
    )

    result = direct_completed_entry_state(task)

    assert result == {
        "ok": True,
        "status": "completed",
        "task_id": "task-1",
        "task_phase": "completed",
        "task_status": "completed",
        "artifacts": {"final_outputs_exist": True, "final_grid_map_exists": True},
    }


def test_executor_prompt_contains_only_plan_identity_and_compact_state(monkeypatch):
    captured = {}

    async def fake_stream(_agent, prompt, **_kwargs):
        captured["prompt"] = prompt
        return "done"

    monkeypatch.setattr(
        "vla_data_juicer_agents.navigation.workflow._run_agent_stream",
        fake_stream,
    )
    plan = SimpleNamespace(plan_id="plan-1", plan_revision=3)

    result = asyncio.run(
        run_executor_agent(
            _SilentAgent(),
            plan,
            execution_overview={"current_step_id": "step-1", "completed_steps": 0},
        )
    )

    assert result == "done"
    assert "plan-1" in captured["prompt"]
    assert "step-1" in captured["prompt"]
    assert "decisions" not in captured["prompt"]
    assert '"steps": [' not in captured["prompt"]


def test_agent_instructions_preserve_progress_and_language_contract():
    assert "Progress:" in PLAN_AGENT_INSTRUCTIONS
    assert "response_language" in PLAN_AGENT_INSTRUCTIONS
    assert "Progress:" in EXECUTOR_AGENT_INSTRUCTIONS

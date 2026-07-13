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
from vla_data_juicer_agents.navigation.plan_models import ExtractSyncPlanInput
from vla_data_juicer_agents.navigation.plan_store import SqliteNavigationPlanRepository
from vla_data_juicer_agents.navigation.services import NavigationServices
from vla_data_juicer_agents.navigation.evidence_store import FileNavigationEvidenceStore
from vla_data_juicer_agents.navigation.observation_store import SqliteNavigationObservationStore
from vla_data_juicer_agents.navigation.task_store import SqliteNavigationTaskStore
from vla_data_juicer_agents.navigation.config import NavigationSettings
from vla_data_juicer_agents.navigation.workflow import (
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

    def get_active_for_task(self, task_id):
        self.calls.append(task_id)
        return self.plan


def _stored_extract_plan() -> ExtractSyncPlanInput:
    return ExtractSyncPlanInput.model_validate(
        {
            "decisions": {
                "sensor_bindings": {
                    "bindings": {"fisheye_front": "/camera", "lidar": "/lidar", "odom": "/odom"},
                    "reason": "observed",
                    "evidence_refs": ["evidence:sensors"],
                },
                "topic_selection": {
                    "topic_whitelist": ["/camera", "/lidar", "/odom"],
                    "topic_map": {"/camera": "fisheye_front", "/lidar": "lidar", "/odom": "odom"},
                    "query_dir": "/query",
                    "reason": "observed",
                    "evidence_refs": ["evidence:topics"],
                },
                "time_sync": {
                    "reference_sensor": "lidar",
                    "method": "nearest_timestamp",
                    "tolerance_ms": 50,
                    "reason": "observed",
                    "evidence_refs": ["evidence:timing"],
                },
            },
            "steps": [
                {
                    "step_id": "prepare",
                    "action": "prepare_raw_data",
                    "variant": "default",
                    "arguments": {},
                    "depends_on": [],
                    "failure_policy": "stop",
                    "decision_refs": [],
                }
            ],
        }
    )


def _real_terminal_services(tmp_path, *, dry_run: bool):
    settings = NavigationSettings(vladatasets_root=tmp_path / "data")
    db_path = tmp_path / "navigation.sqlite"
    task_store = SqliteNavigationTaskStore(db_path)
    task = task_store.create_task_attempt(
        request="Direct navigation workflow",
        target="20270605",
        date="20270605",
        segments=["segment-1"],
        scene_mode=None,
        dry_run=dry_run,
        web_session_id="direct:test-run",
        agentscope_session_id="direct-session",
    ).task
    plan_store = SqliteNavigationPlanRepository(db_path)
    plan = plan_store.activate(
        task, "extract_sync", 1, _stored_extract_plan(),
        expected_web_session_id="direct:test-run",
        expected_agentscope_session_id="direct-session",
    )
    services = NavigationServices(
        settings=settings,
        task_store=task_store,
        observation_store=SqliteNavigationObservationStore(db_path),
        evidence_store=FileNavigationEvidenceStore(tmp_path / "evidence"),
        plan_store=plan_store,
    )
    return services, task, plan


def test_create_plan_agent_uses_only_resolved_tools(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    tools = [SimpleNamespace(name="get_navigation_task_context_tool")]

    agent = create_plan_agent(tools=tools)

    assert agent.tools == tools
    assert "complete JSON Plan" in agent.instructions
    assert "data_profile_draft" not in agent.instructions


def test_plan_agent_prompt_keeps_model_ownership_without_phase_routing():
    for contract in [
        "investigate current products before deciding",
        "choose which inspection tools",
        "choose the processing stage",
        "Both complete-plan submission tools",
        "steps, variants, and business parameters",
        "resubmit the whole complete JSON Plan",
    ]:
        assert contract in PLAN_AGENT_INSTRUCTIONS

    for obsolete in [
        "current durable phase",
        "active phase",
        "one submission tool",
        "reconcile artifacts",
    ]:
        assert obsolete not in PLAN_AGENT_INSTRUCTIONS.lower()


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
        )
    )

    assert plan is stored
    assert store.calls == ["task-1"]


def test_run_plan_agent_rejects_assistant_text_without_stored_plan():
    with pytest.raises(RuntimeError, match="did not submit a valid complete plan"):
        asyncio.run(
            run_plan_agent(
                _SilentAgent(),
                NavigationRequest(date="20270605"),
                plan_store=_PlanStore(),
                task_id="task-1",
            )
        )


def test_direct_planning_refreshes_activity_tools_after_durable_observation_progress(
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
    assert task.created_by_web_session_id == "direct:run"
    assert task.agentscope_session_id == "direct-session"
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
    ("task_status", "current_step", "expected_status", "ok"),
    [
        ("failed", "step-1", "failed", False),
        ("needs_replan", "step-1", "needs_replan", False),
        ("waiting_user", "confirm", "waiting_user", False),
        ("active", "step-1", "incomplete", False),
        ("completed", None, "completed", True),
    ],
)
def test_direct_terminal_state_is_derived_from_durable_ledger(
    task_status, current_step, expected_status, ok
):
    task = SimpleNamespace(
        task_id="task-1",
        status=SimpleNamespace(value=task_status),
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


def test_direct_terminal_treats_completed_dry_run_ledger_as_success(tmp_path):
    services, task, plan = _real_terminal_services(tmp_path, dry_run=True)
    assert services.plan_store.claim_step(
        plan.plan_id, "prepare", "prepare_raw_data",
        expected_web_session_id="direct:test-run",
        expected_agentscope_session_id="direct-session",
    )
    services.plan_store.stage_step_result(
        plan.plan_id,
        "prepare",
        target_status="completed",
        full_result={"ok": True, "tool_name": "prepare_raw_data", "message": "dry run"},
        result_summary={"ok": True, "tool_name": "prepare_raw_data", "message": "dry run"},
        expected_web_session_id="direct:test-run",
        expected_agentscope_session_id="direct-session",
    )
    assert services.plan_store.finalize_staged_step(
        plan.plan_id, "prepare",
        expected_web_session_id="direct:test-run",
        expected_agentscope_session_id="direct-session",
    )

    result = direct_execution_terminal_state(
        services=services,
        task_id=task.task_id,
        plan_id=plan.plan_id,
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["dry_run"] is True


def test_direct_terminal_prefers_real_waiting_ledger_over_pending_task(tmp_path):
    services, task, plan = _real_terminal_services(tmp_path, dry_run=False)
    assert services.plan_store.mark_waiting_user(
        plan.plan_id, "prepare", "prepare_raw_data",
        expected_web_session_id="direct:test-run",
        expected_agentscope_session_id="direct-session",
    )

    result = direct_execution_terminal_state(
        services=services,
        task_id=task.task_id,
        plan_id=plan.plan_id,
    )

    assert result["ok"] is False
    assert result["status"] == "waiting_user"


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

from datetime import datetime, timedelta, timezone

from vla_data_juicer_agents.tui.event_adapter import apply_event
from vla_data_juicer_agents.tui.models import TuiState


def event(
    event_type: str,
    source: str,
    *,
    run_id: str = "run_1",
    parent_run_id: str | None = "parent_1",
    timestamp: datetime | None = None,
    **payload,
):
    return {
        "type": event_type,
        "source": source,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        "payload": payload,
    }


def test_reasoning_uses_source_and_natural_text_without_round_labels():
    state = TuiState()

    apply_event(
        state,
        event("reasoning", "navigation.plan", summary="先检查原始片段。"),
    )

    item = state.timeline[-1]
    assert item.kind == "reasoning"
    assert item.source_label == "Plan"
    assert item.text == "先检查原始片段。"
    assert "思考摘要" not in item.text
    assert "第 1 轮" not in item.text


def test_tool_start_drives_spinner_and_tool_end_adds_timeline_item():
    state = TuiState()
    started = datetime(2026, 6, 21, 10, 0, tzinfo=timezone.utc)

    apply_event(
        state,
        event(
            "tool_start",
            "navigation.executor",
            timestamp=started,
            call_id="c1",
            tool="prepare_raw_data_tool",
            args='{"date": "20270605"}',
        ),
    )

    assert state.spinner_text() == "[Executor] running prepare_raw_data_tool"
    assert state.timeline == []

    apply_event(
        state,
        event(
            "tool_end",
            "navigation.executor",
            timestamp=started + timedelta(seconds=2),
            call_id="c1",
            tool="prepare_raw_data_tool",
            status="completed",
            summary="prepared",
        ),
    )

    assert state.active_tools == {}
    assert state.timeline[-1].status == "completed"
    assert state.timeline[-1].elapsed_sec == 2
    assert state.timeline[-1].text == "prepared"


def test_spinner_prefers_oldest_tool_then_deepest_active_agent():
    state = TuiState()
    apply_event(
        state,
        event(
            "agent_start",
            "main",
            run_id="main_run",
            parent_run_id=None,
        ),
    )
    apply_event(
        state,
        event(
            "agent_start",
            "navigation.plan",
            run_id="plan_run",
            parent_run_id="main_run",
        ),
    )
    assert state.spinner_text() == "[Plan] thinking"

    apply_event(
        state,
        event(
            "tool_start",
            "navigation.plan",
            run_id="plan_run",
            parent_run_id="main_run",
            call_id="later",
            tool="classify",
        ),
    )
    apply_event(
        state,
        event(
            "tool_start",
            "navigation.plan",
            run_id="plan_run",
            parent_run_id="main_run",
            call_id="newer",
            tool="inspect",
        ),
    )
    assert state.spinner_text() == "[Plan] running classify"


def test_agent_end_removes_active_agent_and_adds_lifecycle_item():
    state = TuiState()
    apply_event(
        state,
        event(
            "agent_start",
            "navigation.workflow",
            run_id="workflow",
            parent_run_id="main",
        ),
    )
    apply_event(
        state,
        event(
            "agent_end",
            "navigation.workflow",
            run_id="workflow",
            parent_run_id="main",
            status="interrupted",
        ),
    )

    assert state.active_agents == {}
    assert state.timeline[-1].kind == "agent"
    assert state.timeline[-1].status == "interrupted"
    assert state.timeline[-1].source_label == "Workflow"


def test_final_appends_one_assistant_message_and_records_stop():
    state = TuiState()
    final = event("final", "main", text="Session ended.", stop=True)

    apply_event(state, final)
    apply_event(state, final)

    assert state.stop is True
    assert [item.kind for item in state.timeline] == ["assistant"]
    assert state.timeline[0].text == "Session ended."


def test_failed_tool_without_start_is_still_displayed():
    state = TuiState()

    apply_event(
        state,
        event(
            "tool_end",
            "navigation.executor",
            call_id="missing",
            tool="run_tracking_tool",
            status="failed",
            error_type="RuntimeError",
            summary="tracking failed",
        ),
    )

    item = state.timeline[-1]
    assert item.kind == "tool"
    assert item.status == "failed"
    assert item.text == "tracking failed"


def test_unknown_event_adds_muted_system_item():
    state = TuiState()

    apply_event(state, event("custom_notice", "navigation.plan", detail="x"))

    item = state.timeline[-1]
    assert item.kind == "system"
    assert item.text == "custom_notice"

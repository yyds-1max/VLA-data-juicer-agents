import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from vla_data_juicer_agents.tui.controller import SessionController
from vla_data_juicer_agents.tui.event_adapter import apply_event
from vla_data_juicer_agents.tui.models import TuiState


def _wait_until_idle(controller: SessionController, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while controller.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert controller.is_running is False


def _event(
    event_type: str,
    source: str,
    *,
    run_id: str,
    parent_run_id: str | None,
    timestamp: datetime,
    **payload,
):
    return {
        "type": event_type,
        "source": source,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "timestamp": timestamp.isoformat(),
        "payload": payload,
    }


class FakeMainAgent:
    def __init__(self, event_callback):
        self.event_callback = event_callback
        self.messages = []

    def prepare_turn(self):
        pass

    def handle_message(self, message):
        self.messages.append(message)
        turn = len(self.messages)
        base = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=turn)
        main_run = f"main_{turn}"
        workflow_run = f"workflow_{turn}"
        plan_run = f"plan_{turn}"
        executor_run = f"executor_{turn}"
        call_id = f"vla_run_workflow_{turn}"
        events = [
            _event(
                "reasoning",
                "main",
                run_id=main_run,
                parent_run_id=None,
                timestamp=base,
                summary="Inspecting the request and preparing workflow execution.",
            ),
            _event(
                "tool_start",
                "main",
                run_id=main_run,
                parent_run_id=None,
                timestamp=base + timedelta(milliseconds=1),
                call_id=call_id,
                tool="vla_run_workflow",
                args='{"date": "20270605", "dry_run": true}',
            ),
            _event(
                "agent_start",
                "navigation.workflow",
                run_id=workflow_run,
                parent_run_id=main_run,
                timestamp=base + timedelta(milliseconds=2),
            ),
            _event(
                "agent_start",
                "navigation.plan",
                run_id=plan_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=3),
            ),
            _event(
                "reasoning",
                "navigation.plan",
                run_id=plan_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=4),
                summary="Inspect source metadata and choose the navigation data profile.",
            ),
            _event(
                "tool_start",
                "navigation.plan",
                run_id=plan_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=5),
                call_id=f"profile_{turn}",
                tool="inspect_navigation_profile",
            ),
            _event(
                "tool_end",
                "navigation.plan",
                run_id=plan_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=6),
                call_id=f"profile_{turn}",
                tool="inspect_navigation_profile",
                status="completed",
                summary="selected go2w_like profile",
            ),
            _event(
                "agent_end",
                "navigation.plan",
                run_id=plan_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=7),
                status="completed",
            ),
            _event(
                "agent_start",
                "navigation.executor",
                run_id=executor_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=8),
            ),
            _event(
                "reasoning",
                "navigation.executor",
                run_id=executor_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=9),
                summary="Run the approved dry-run execution steps.",
            ),
            _event(
                "tool_start",
                "navigation.executor",
                run_id=executor_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=10),
                call_id=f"execute_{turn}",
                tool="prepare_raw_data",
            ),
            _event(
                "tool_end",
                "navigation.executor",
                run_id=executor_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=11),
                call_id=f"execute_{turn}",
                tool="prepare_raw_data",
                status="completed",
                summary="dry-run command recorded",
            ),
            _event(
                "agent_end",
                "navigation.executor",
                run_id=executor_run,
                parent_run_id=workflow_run,
                timestamp=base + timedelta(milliseconds=12),
                status="completed",
            ),
            _event(
                "agent_end",
                "navigation.workflow",
                run_id=workflow_run,
                parent_run_id=main_run,
                timestamp=base + timedelta(milliseconds=13),
                status="completed",
            ),
            _event(
                "tool_end",
                "main",
                run_id=main_run,
                parent_run_id=None,
                timestamp=base + timedelta(milliseconds=14),
                call_id=call_id,
                tool="vla_run_workflow",
                status="completed",
                summary="workflow completed",
            ),
            _event(
                "final",
                "main",
                run_id=main_run,
                parent_run_id=None,
                timestamp=base + timedelta(milliseconds=15),
                text=f"Turn {turn} complete.",
                stop=False,
            ),
        ]
        for event in events:
            self.event_callback(event)
        return SimpleNamespace(text=f"Turn {turn} complete.", stop=False, interrupted=False)

    def request_interrupt(self, *, allow_pending=False):
        del allow_pending
        return True


def _run_turn(controller: SessionController, state: TuiState, message: str):
    controller.submit_turn(message)
    _wait_until_idle(controller)
    events = controller.drain_events()
    for event in events:
        apply_event(state, event)
    result = controller.consume_turn_result()
    return result, events


def test_controller_events_render_approved_main_workflow_plan_executor_chain():
    created = []

    def factory(**kwargs):
        agent = FakeMainAgent(kwargs["event_callback"])
        created.append(agent)
        return agent

    controller = SessionController(agent_factory=factory)
    state = TuiState()
    controller.start()

    result, events = _run_turn(controller, state, "run navigation workflow")

    assert result.text == "Turn 1 complete."
    assert len(created) == 1
    assert [event["source"] for event in events] == [
        "main",
        "main",
        "navigation.workflow",
        "navigation.plan",
        "navigation.plan",
        "navigation.plan",
        "navigation.plan",
        "navigation.plan",
        "navigation.executor",
        "navigation.executor",
        "navigation.executor",
        "navigation.executor",
        "navigation.executor",
        "navigation.workflow",
        "main",
        "main",
    ]
    assert [(event["type"], event["source"]) for event in events] == [
        ("reasoning", "main"),
        ("tool_start", "main"),
        ("agent_start", "navigation.workflow"),
        ("agent_start", "navigation.plan"),
        ("reasoning", "navigation.plan"),
        ("tool_start", "navigation.plan"),
        ("tool_end", "navigation.plan"),
        ("agent_end", "navigation.plan"),
        ("agent_start", "navigation.executor"),
        ("reasoning", "navigation.executor"),
        ("tool_start", "navigation.executor"),
        ("tool_end", "navigation.executor"),
        ("agent_end", "navigation.executor"),
        ("agent_end", "navigation.workflow"),
        ("tool_end", "main"),
        ("final", "main"),
    ]
    assert {event["run_id"]: event["parent_run_id"] for event in events} == {
        "main_1": None,
        "workflow_1": "main_1",
        "plan_1": "workflow_1",
        "executor_1": "workflow_1",
    }

    tool_completions = [
        item for item in state.timeline if item.kind == "tool" and item.tool == "vla_run_workflow"
    ]
    assert len(tool_completions) == 1
    assert tool_completions[0].source_label == "Main"
    assert tool_completions[0].status == "completed"
    assert state.active_agents == {}
    assert state.active_tools == {}

    labels_and_text = [(item.source_label, item.text) for item in state.timeline]
    assert labels_and_text == [
        ("Main", "Inspecting the request and preparing workflow execution."),
        ("Workflow", "started"),
        ("Plan", "started"),
        ("Plan", "Inspect source metadata and choose the navigation data profile."),
        ("Plan", "selected go2w_like profile"),
        ("Plan", "completed"),
        ("Executor", "started"),
        ("Executor", "Run the approved dry-run execution steps."),
        ("Executor", "dry-run command recorded"),
        ("Executor", "completed"),
        ("Workflow", "completed"),
        ("Main", "workflow completed"),
        ("Main", "Turn 1 complete."),
    ]
    forbidden_labels = ("reasoning_step", "思考摘要", "第 N 轮", "第 1 轮", "汇总")
    rendered = "\n".join(f"{item.source_label} {item.text}" for item in state.timeline)
    for forbidden in forbidden_labels:
        assert forbidden not in rendered
    assert sum(item.kind == "assistant" for item in state.timeline) == 1

    second_result, second_events = _run_turn(controller, state, "run again")

    assert second_result.text == "Turn 2 complete."
    assert [event["run_id"] for event in second_events if event["source"] == "main"] == [
        "main_2",
        "main_2",
        "main_2",
        "main_2",
    ]
    assert sum(item.kind == "assistant" for item in state.timeline) == 2

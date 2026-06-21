import json
from datetime import datetime, timezone

import pytest

from vla_data_juicer_agents.core.events import (
    CallbackEventSink,
    CompositeEventSink,
    EventEmitter,
    JsonlEventSink,
)


def test_child_scope_emits_serializable_parent_linked_event():
    captured = []
    emitter = EventEmitter(CallbackEventSink(captured.append))
    parent = emitter.scope("coordinator", run_id="parent-run")
    child = parent.child("worker", run_id="child-run")

    event = child.emit("tool.started", {"message": "processing"})

    assert captured == [event]
    assert captured[0] is not event
    assert event["type"] == "tool.started"
    assert event["source"] == "worker"
    assert event["run_id"] == "child-run"
    assert event["parent_run_id"] == "parent-run"
    assert event["payload"] == {"message": "processing"}
    timestamp = datetime.fromisoformat(event["timestamp"])
    assert timestamp.tzinfo == timezone.utc
    assert len(event["timestamp"].rsplit(".", 1)[1]) == 9  # milliseconds plus +00:00
    json.dumps(event)


def test_failing_sink_does_not_prevent_later_sink(caplog):
    captured = []

    class FailingSink:
        def publish(self, event):
            raise RuntimeError("sink failed")

    emitter = EventEmitter(FailingSink(), CallbackEventSink(captured.append))

    event = emitter.scope("coordinator", run_id="run-1").emit("run.started", {})

    assert captured == [event]
    assert "sink failed" in caplog.text


def test_jsonl_sink_writes_one_json_object_per_event(tmp_path):
    path = tmp_path / "nested" / "events.jsonl"
    scope = EventEmitter(JsonlEventSink(path)).scope("worker", run_id="run-1")

    first = scope.emit("step.started", {"message": "处理中"})
    second = scope.emit("step.finished", {"index": 1})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "处理中" in lines[0]
    assert [json.loads(line) for line in lines] == [first, second]


def test_emitter_and_composite_accept_iterable_sinks():
    first = []
    second = []
    composite = CompositeEventSink(
        [CallbackEventSink(first.append), CallbackEventSink(second.append)]
    )
    emitter = EventEmitter([composite])

    event = emitter.scope("worker", run_id="run-1").emit("step.started", {})

    assert first == [event]
    assert second == [event]


def test_scope_emit_accepts_keyword_payload():
    captured = []
    child = EventEmitter([CallbackEventSink(captured.append)]).scope(
        "worker", run_id="run-1"
    )

    event = child.emit("reasoning", summary="text")

    assert event["payload"] == {"summary": "text"}
    assert captured == [event]


def test_event_transform_sanitizes_returned_event_and_all_sinks_once():
    first = []
    second = []
    transform_calls = []

    def redact(event):
        transform_calls.append(event)
        return {
            **event,
            "payload": {"summary": event["payload"]["summary"].replace("hunter2", "[REDACTED]")},
        }

    emitter = EventEmitter(
        CallbackEventSink(first.append),
        CallbackEventSink(second.append),
        event_transform=redact,
    )

    event = emitter.scope("worker", run_id="run-1").emit(
        "reasoning",
        summary="password=hunter2",
    )

    assert len(transform_calls) == 1
    assert event["payload"] == {"summary": "password=[REDACTED]"}
    assert first == [event]
    assert second == [event]


def test_with_sink_preserves_event_transform(tmp_path):
    captured = []
    path = tmp_path / "events.jsonl"

    def redact(event):
        return {
            **event,
            "payload": {"summary": event["payload"]["summary"].replace("hunter2", "[REDACTED]")},
        }

    emitter = EventEmitter(
        CallbackEventSink(captured.append),
        event_transform=redact,
    ).with_sink(JsonlEventSink(path))

    event = emitter.scope("worker", run_id="run-1").emit(
        "reasoning",
        summary="password=hunter2",
    )

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert captured == [event]
    assert persisted == event
    assert "hunter2" not in json.dumps({"captured": captured, "persisted": persisted})


def test_jsonl_sink_does_not_write_partial_line_when_serialization_fails(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JsonlEventSink(path)

    with pytest.raises(TypeError):
        sink.publish({"payload": object()})
    sink.publish({"type": "valid", "payload": {}})

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"type": "valid", "payload": {}}]


def test_sink_constructors_flatten_each_iterable_argument():
    first = []
    second = []
    third = []
    fourth = []
    emitter = EventEmitter(
        [CallbackEventSink(first.append)], CallbackEventSink(second.append)
    )
    composite = CompositeEventSink(
        [CallbackEventSink(third.append)], [CallbackEventSink(fourth.append)]
    )

    event = EventEmitter(emitter, composite).scope("worker", run_id="run-1").emit(
        "step.started", {}
    )

    assert first == [event]
    assert second == [event]
    assert third == [event]
    assert fourth == [event]

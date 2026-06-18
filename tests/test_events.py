import json
from datetime import datetime, timezone

from vla_data_juicer_agents.core.events import (
    CallbackEventSink,
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

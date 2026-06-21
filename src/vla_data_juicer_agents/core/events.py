"""Transport-neutral events emitted by agent workflows."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4


logger = logging.getLogger(__name__)

Event = dict[str, Any]
EventTransform = Callable[[Event], Mapping[str, Any]]


class EventSink(Protocol):
    """Destination for normalized agent events."""

    def publish(self, event: Mapping[str, Any]) -> None:
        """Publish an event."""


SinkInput = EventSink | Iterable[EventSink]


def _normalize_sinks(sinks: tuple[SinkInput, ...]) -> tuple[EventSink, ...]:
    normalized = []
    for sink in sinks:
        if hasattr(sink, "publish"):
            normalized.append(cast(EventSink, sink))
        else:
            normalized.extend(cast(Iterable[EventSink], sink))
    return tuple(normalized)


class CallbackEventSink:
    """Deliver events to an in-process callback."""

    def __init__(self, callback: Callable[[Event], None]) -> None:
        self._callback = callback

    def publish(self, event: Mapping[str, Any]) -> None:
        self._callback(dict(event))


class JsonlEventSink:
    """Append events to a UTF-8 JSON Lines file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def publish(self, event: Mapping[str, Any]) -> None:
        serialized = json.dumps(dict(event), ensure_ascii=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as output:
            output.write(serialized + "\n")


class CompositeEventSink:
    """Publish to every sink, isolating failures between destinations."""

    def __init__(self, *sinks: SinkInput) -> None:
        self._sinks = _normalize_sinks(sinks)

    def publish(self, event: Mapping[str, Any]) -> None:
        for sink in self._sinks:
            try:
                sink.publish(event)
            except Exception:
                logger.exception("Event sink failed: %r", sink)


class EventEmitter:
    """Publish normalized events to one or more sinks."""

    def __init__(
        self,
        *sinks: SinkInput,
        event_transform: EventTransform | None = None,
    ) -> None:
        self._sinks = _normalize_sinks(sinks)
        self._composite = CompositeEventSink(*self._sinks)
        self._event_transform = event_transform or (lambda event: event)

    def with_sink(self, sink: EventSink) -> EventEmitter:
        return EventEmitter(
            *self._sinks,
            sink,
            event_transform=self._event_transform,
        )

    def scope(
        self,
        source: str,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> EventScope:
        return EventScope(
            emitter=self,
            source=source,
            run_id=run_id or str(uuid4()),
            parent_run_id=parent_run_id,
        )

    def publish(self, event: Mapping[str, Any]) -> Event:
        published = dict(self._event_transform(dict(event)))
        self._composite.publish(published)
        return published


@dataclass(frozen=True)
class EventScope:
    """Source and run context used to construct normalized events."""

    emitter: EventEmitter
    source: str
    run_id: str
    parent_run_id: str | None = None

    def child(self, source: str, run_id: str | None = None) -> EventScope:
        return self.emitter.scope(
            source=source,
            run_id=run_id,
            parent_run_id=self.run_id,
        )

    def emit(
        self,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        **payload_fields: Any,
    ) -> Event:
        normalized_payload = dict(payload) if payload is not None else {}
        normalized_payload.update(payload_fields)
        event = {
            "type": event_type,
            "source": self.source,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "payload": normalized_payload,
        }
        return self.emitter.publish(event)

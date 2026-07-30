import { act, render } from "@testing-library/react";

import {
  ANNOTATION_EVENT_DEBOUNCE_MS,
  ANNOTATION_RECONCILE_INTERVAL_MS,
  type AnnotationDomainEvent,
  useAnnotationEvents,
} from "./events";

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onopen: ((event: Event) => unknown) | null = null;
  onmessage: ((event: MessageEvent<string>) => unknown) | null = null;
  closed = false;
  private readonly listeners = new Map<string, EventListener[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  close() {
    this.closed = true;
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  open() {
    this.onopen?.(new Event("open"));
  }

  emit(event: AnnotationDomainEvent) {
    const message = new MessageEvent("annotation", {
      data: JSON.stringify(event),
    });
    for (const listener of this.listeners.get("annotation") ?? []) {
      listener(message);
    }
  }
}

function event(
  seq: number,
  overrides: Partial<AnnotationDomainEvent> = {},
): AnnotationDomainEvent {
  return {
    seq,
    event_ref: `annotation_event_${seq}`,
    event_kind: "annotation.job.changed",
    aggregate_kind: "job",
    job_ref: "job_target",
    state_revision: seq,
    status: "tracking",
    occurred_at: "2026-07-29T00:00:00Z",
    ...overrides,
  };
}

function Probe({
  onEvent,
  onReconcile,
  reconcileIntervalMs,
}: {
  onEvent: (next: AnnotationDomainEvent) => void;
  onReconcile: () => void;
  reconcileIntervalMs?: number;
}) {
  useAnnotationEvents({
    filter: (next) => next.job_ref === "job_target",
    onEvent,
    onReconcile,
    reconcileIntervalMs,
  });
  return null;
}

function UnfilteredProbe({
  onEvent,
  onReconcile = () => undefined,
}: {
  onEvent: (next: AnnotationDomainEvent) => void;
  onReconcile?: () => void;
}) {
  useAnnotationEvents({ onEvent, onReconcile });
  return null;
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ cursor: 12 }),
  }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("subscribes after the server cursor, filters unrelated events, and ignores old seq", async () => {
  vi.useFakeTimers();
  const onEvent = vi.fn();
  const onReconcile = vi.fn();
  const { unmount } = render(
    <Probe onEvent={onEvent} onReconcile={onReconcile} />,
  );

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(FakeEventSource.instances).toHaveLength(1);
  const source = FakeEventSource.instances[0];
  expect(source.url).toBe("/api/annotation/events?after_seq=12");

  act(() => source.open());
  expect(onReconcile).toHaveBeenCalledTimes(1);

  act(() => {
    source.emit(event(13, { job_ref: "job_unrelated" }));
    source.emit(event(14));
    source.emit(event(14, { state_revision: 15 }));
    source.emit(event(11));
    vi.advanceTimersByTime(ANNOTATION_EVENT_DEBOUNCE_MS);
  });

  expect(onEvent).toHaveBeenCalledTimes(1);
  expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ seq: 14 }));

  act(() => window.dispatchEvent(new Event("focus")));
  expect(onReconcile).toHaveBeenCalledTimes(2);

  unmount();
  expect(source.closed).toBe(true);
});

test("coalesces a burst of relevant named events into one invalidation", async () => {
  vi.useFakeTimers();
  const onEvent = vi.fn();
  render(<Probe onEvent={onEvent} onReconcile={vi.fn()} />);

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  const source = FakeEventSource.instances[0];

  act(() => {
    source.emit(event(13));
    source.emit(event(14));
    source.emit(event(15));
    vi.advanceTimersByTime(ANNOTATION_EVENT_DEBOUNCE_MS - 1);
  });
  expect(onEvent).not.toHaveBeenCalled();

  act(() => {
    vi.advanceTimersByTime(1);
  });
  expect(onEvent).toHaveBeenCalledTimes(1);
  expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ seq: 15 }));
});

test("retains every affected aggregate in one debounce batch", async () => {
  vi.useFakeTimers();
  const onEvent = vi.fn();
  render(<UnfilteredProbe onEvent={onEvent} />);

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  const source = FakeEventSource.instances[0];

  act(() => {
    source.emit(event(13, { job_ref: "job_a" }));
    source.emit(event(14, { job_ref: "job_b" }));
    source.emit(event(15, {
      aggregate_kind: "segment",
      job_ref: "job_a",
      segment_ref: "segment_c",
    }));
    source.emit(event(16, { job_ref: "job_a", state_revision: 16 }));
    vi.advanceTimersByTime(ANNOTATION_EVENT_DEBOUNCE_MS);
  });

  expect(onEvent).toHaveBeenCalledTimes(3);
  expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({
    aggregate_kind: "job",
    job_ref: "job_a",
    seq: 16,
  }));
  expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({
    aggregate_kind: "job",
    job_ref: "job_b",
  }));
  expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({
    aggregate_kind: "segment",
    segment_ref: "segment_c",
  }));
});

test("shares one EventSource across route-level subscribers", async () => {
  const first = vi.fn();
  const second = vi.fn();
  render(
    <>
      <UnfilteredProbe onEvent={first} />
      <UnfilteredProbe onEvent={second} />
    </>,
  );

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  expect(FakeEventSource.instances).toHaveLength(1);
});

test("reconciles when the event stream or browser connectivity recovers", async () => {
  const onReconcile = vi.fn();
  render(<UnfilteredProbe onEvent={vi.fn()} onReconcile={onReconcile} />);

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  const source = FakeEventSource.instances[0];

  act(() => source.open());
  act(() => window.dispatchEvent(new Event("online")));
  expect(onReconcile).toHaveBeenCalledTimes(2);
});

test("uses a low-frequency reconciliation fallback", async () => {
  vi.useFakeTimers();
  const onReconcile = vi.fn();
  render(
    <Probe
      onEvent={vi.fn()}
      onReconcile={onReconcile}
      reconcileIntervalMs={ANNOTATION_RECONCILE_INTERVAL_MS}
    />,
  );

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });

  const callsBeforeInterval = onReconcile.mock.calls.length;
  act(() => {
    vi.advanceTimersByTime(ANNOTATION_RECONCILE_INTERVAL_MS);
  });
  expect(onReconcile).toHaveBeenCalledTimes(callsBeforeInterval + 1);
});

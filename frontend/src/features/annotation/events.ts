import { useEffect, useRef } from "react";

const ANNOTATION_EVENTS_ROOT = "/api/annotation/events";

export const ANNOTATION_RECONCILE_INTERVAL_MS = 60_000;
export const ANNOTATION_EVENT_DEBOUNCE_MS = 80;

export type AnnotationDomainEvent = {
  seq: number;
  event_ref: string;
  event_kind: string;
  aggregate_kind: "job" | "segment" | "review";
  job_ref?: string;
  segment_ref?: string;
  review_ref?: string;
  state_revision: number;
  status: string;
  occurred_at: string;
};

type AnnotationEventOptions = {
  enabled?: boolean;
  filter?: (event: AnnotationDomainEvent) => boolean;
  onEvent: (event: AnnotationDomainEvent) => void | Promise<void>;
  onReconcile: () => void | Promise<void>;
  reconcileIntervalMs?: number;
};

type AnnotationEventSubscriber = {
  onEvents: (events: AnnotationDomainEvent[]) => void;
  onReconcile: () => void;
};

const subscribers = new Set<AnnotationEventSubscriber>();
let retainCount = 0;
let source: EventSource | null = null;
let cursorController: AbortController | null = null;
let reconcileTimer: number | null = null;
let eventTimer: number | null = null;
let lastSeq = -1;
let reconcileInterval = ANNOTATION_RECONCILE_INTERVAL_MS;
const pendingEvents = new Map<string, AnnotationDomainEvent>();

function safeInvoke(callback: () => void | Promise<void>) {
  try {
    void Promise.resolve(callback()).catch(() => undefined);
  } catch {
    // Projection owners surface their own read failures.
  }
}

export function parseAnnotationDomainEvent(raw: string): AnnotationDomainEvent | null {
  try {
    const value = JSON.parse(raw) as Partial<AnnotationDomainEvent>;
    if (
      !Number.isSafeInteger(value.seq)
      || (value.seq ?? -1) < 0
      || typeof value.event_ref !== "string"
      || typeof value.event_kind !== "string"
      || !(
        value.aggregate_kind === "job"
        || value.aggregate_kind === "segment"
        || value.aggregate_kind === "review"
      )
      || !Number.isSafeInteger(value.state_revision)
      || typeof value.status !== "string"
      || typeof value.occurred_at !== "string"
    ) {
      return null;
    }
    return value as AnnotationDomainEvent;
  } catch {
    return null;
  }
}

async function getAnnotationEventCursor(signal: AbortSignal): Promise<number> {
  const response = await fetch(`${ANNOTATION_EVENTS_ROOT}/cursor`, {
    headers: { accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new Error(`Annotation event cursor unavailable (${response.status})`);
  }
  const body = await response.json() as { cursor?: unknown };
  if (!Number.isSafeInteger(body.cursor) || Number(body.cursor) < 0) {
    throw new Error("Annotation event cursor is invalid");
  }
  return Number(body.cursor);
}

function aggregateIdentity(event: AnnotationDomainEvent): string {
  if (event.aggregate_kind === "review") {
    return `review:${event.review_ref ?? event.event_ref}`;
  }
  if (event.aggregate_kind === "segment") {
    return `segment:${event.segment_ref ?? event.event_ref}`;
  }
  return `job:${event.job_ref ?? event.event_ref}`;
}

function publishReconcile() {
  for (const subscriber of subscribers) {
    subscriber.onReconcile();
  }
}

function flushPendingEvents() {
  eventTimer = null;
  const events = [...pendingEvents.values()].sort((left, right) => left.seq - right.seq);
  pendingEvents.clear();
  if (events.length === 0) return;
  for (const subscriber of subscribers) {
    subscriber.onEvents(events);
  }
}

function handleAnnotationEvent(rawEvent: Event) {
  const message = rawEvent as MessageEvent<string>;
  const event = parseAnnotationDomainEvent(message.data);
  if (!event || event.seq <= lastSeq) return;
  lastSeq = event.seq;
  const key = aggregateIdentity(event);
  const current = pendingEvents.get(key);
  if (!current || current.seq < event.seq) pendingEvents.set(key, event);
  if (eventTimer !== null) return;
  eventTimer = window.setTimeout(flushPendingEvents, ANNOTATION_EVENT_DEBOUNCE_MS);
}

function refreshWhenVisible() {
  if (document.visibilityState === "visible") publishReconcile();
}

function startReconcileTimer() {
  if (reconcileTimer !== null) window.clearInterval(reconcileTimer);
  reconcileTimer = window.setInterval(publishReconcile, reconcileInterval);
}

async function startConnection() {
  if (source || cursorController) return;
  const controller = new AbortController();
  cursorController = controller;
  let cursor = 0;
  try {
    cursor = await getAnnotationEventCursor(controller.signal);
  } catch {
    if (controller.signal.aborted) return;
  }
  if (cursorController !== controller) {
    return;
  }
  if (retainCount === 0 || typeof EventSource === "undefined") {
    cursorController = null;
    return;
  }

  lastSeq = Math.max(lastSeq, cursor);
  source = new EventSource(
    `${ANNOTATION_EVENTS_ROOT}?after_seq=${encodeURIComponent(String(lastSeq))}`,
  );
  source.onopen = publishReconcile;
  if (typeof source.addEventListener === "function") {
    source.addEventListener("annotation", handleAnnotationEvent);
  } else {
    source.onmessage = handleAnnotationEvent;
  }
  cursorController = null;
}

function retainConnection(requestedInterval: number): () => void {
  retainCount += 1;
  if (requestedInterval < reconcileInterval) {
    reconcileInterval = requestedInterval;
    if (retainCount > 1) startReconcileTimer();
  }
  if (retainCount === 1) {
    reconcileInterval = requestedInterval;
    startReconcileTimer();
    window.addEventListener("focus", refreshWhenVisible);
    window.addEventListener("online", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    void startConnection();
  }

  return () => {
    retainCount = Math.max(0, retainCount - 1);
    if (retainCount > 0) return;
    cursorController?.abort();
    cursorController = null;
    source?.close();
    source = null;
    if (eventTimer !== null) window.clearTimeout(eventTimer);
    eventTimer = null;
    pendingEvents.clear();
    if (reconcileTimer !== null) window.clearInterval(reconcileTimer);
    reconcileTimer = null;
    lastSeq = -1;
    reconcileInterval = ANNOTATION_RECONCILE_INTERVAL_MS;
    window.removeEventListener("focus", refreshWhenVisible);
    window.removeEventListener("online", refreshWhenVisible);
    document.removeEventListener("visibilitychange", refreshWhenVisible);
  };
}

/**
 * Subscribes to the process-wide Annotation event connection.
 *
 * AppShell retains the connection for the lifetime of the SPA. Route-level
 * consumers only subscribe to the shared batches, so navigating between jobs
 * and reviews never rebuilds the EventSource.
 */
export function useAnnotationEvents({
  enabled = true,
  filter,
  onEvent,
  onReconcile,
  reconcileIntervalMs = ANNOTATION_RECONCILE_INTERVAL_MS,
}: AnnotationEventOptions) {
  const filterRef = useRef(filter);
  const onEventRef = useRef(onEvent);
  const onReconcileRef = useRef(onReconcile);

  filterRef.current = filter;
  onEventRef.current = onEvent;
  onReconcileRef.current = onReconcile;

  useEffect(() => {
    if (!enabled) return undefined;
    const subscriber: AnnotationEventSubscriber = {
      onEvents: (events) => {
        for (const event of events) {
          if (!filterRef.current || filterRef.current(event)) {
            safeInvoke(() => onEventRef.current(event));
          }
        }
      },
      onReconcile: () => safeInvoke(() => onReconcileRef.current()),
    };
    subscribers.add(subscriber);
    const release = retainConnection(reconcileIntervalMs);
    return () => {
      subscribers.delete(subscriber);
      release();
    };
  }, [enabled, reconcileIntervalMs]);
}

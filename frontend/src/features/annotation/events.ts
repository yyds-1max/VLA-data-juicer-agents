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

function safeInvoke(callback: () => void | Promise<void>) {
  try {
    void Promise.resolve(callback()).catch(() => undefined);
  } catch {
    // A refresh failure is already presented by the owning page.
  }
}

function parseAnnotationDomainEvent(raw: string): AnnotationDomainEvent | null {
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

/**
 * Subscribes to public Annotation domain events.
 *
 * The HTTP resources remain authoritative. Events only invalidate the affected
 * projection, while the reconcile callback closes snapshot/subscribe races and
 * repairs gaps after reconnects or browser suspension.
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

    let cancelled = false;
    let source: EventSource | null = null;
    const cursorController = new AbortController();
    let lastSeq = -1;
    let eventTimer: number | undefined;
    let pendingEvent: AnnotationDomainEvent | null = null;

    const reconcile = () => {
      if (!cancelled) safeInvoke(() => onReconcileRef.current());
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") reconcile();
    };

    const interval = window.setInterval(reconcile, reconcileIntervalMs);
    window.addEventListener("focus", refreshWhenVisible);
    window.addEventListener("online", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);

    const subscribe = async () => {
      let cursor = 0;
      try {
        cursor = await getAnnotationEventCursor(cursorController.signal);
      } catch {
        if (cursorController.signal.aborted) return;
      }
      if (cancelled || typeof EventSource === "undefined") return;

      lastSeq = Math.max(lastSeq, cursor);
      source = new EventSource(
        `${ANNOTATION_EVENTS_ROOT}?after_seq=${encodeURIComponent(String(lastSeq))}`,
      );
      source.onopen = reconcile;
      const handleAnnotationEvent = (rawEvent: Event) => {
        const message = rawEvent as MessageEvent<string>;
        const event = parseAnnotationDomainEvent(message.data);
        if (!event || event.seq <= lastSeq) return;
        lastSeq = event.seq;
        if (filterRef.current && !filterRef.current(event)) return;
        pendingEvent = event;
        if (eventTimer !== undefined) return;
        eventTimer = window.setTimeout(() => {
          eventTimer = undefined;
          const next = pendingEvent;
          pendingEvent = null;
          if (next && !cancelled) {
            safeInvoke(() => onEventRef.current(next));
          }
        }, ANNOTATION_EVENT_DEBOUNCE_MS);
      };
      if (typeof source.addEventListener === "function") {
        source.addEventListener("annotation", handleAnnotationEvent);
      } else {
        // Older test doubles and embedded EventSource shims may only expose
        // the default handler. Production browsers use the named event above.
        source.onmessage = handleAnnotationEvent;
      }
    };

    void subscribe();

    return () => {
      cancelled = true;
      cursorController.abort();
      source?.close();
      if (eventTimer !== undefined) window.clearTimeout(eventTimer);
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshWhenVisible);
      window.removeEventListener("online", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [enabled, reconcileIntervalMs]);
}

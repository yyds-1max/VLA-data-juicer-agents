import { useAnnotationEvents } from "./events";
import {
  reconcileLoadedAnnotationProjections,
  refreshAnnotationProjectionForEvent,
} from "./projectionStore";

export function AnnotationDomainEventBridge() {
  useAnnotationEvents({
    onEvent: refreshAnnotationProjectionForEvent,
    onReconcile: reconcileLoadedAnnotationProjections,
  });
  return null;
}

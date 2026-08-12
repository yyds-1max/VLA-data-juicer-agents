import { useAnnotationEvents } from "./events";
import {
  reconcileLoadedAnnotationProjections,
  refreshAnnotationProjectionForEvent,
} from "./projectionStore";
import { scheduleNavigationDatasetDateRefresh } from "../console/navigationDatasetSummaryCache";

export function AnnotationDomainEventBridge() {
  // 在应用壳层只建立一条标注领域事件连接；页面组件统一读取 projectionStore，
  // 避免任务列表、详情页和工作台各自订阅 SSE 造成重复请求与状态竞争。
  useAnnotationEvents({
    onEvent: async (event) => {
      try {
        await refreshAnnotationProjectionForEvent(event);
      } finally {
        if (event.dataset_date) {
          scheduleNavigationDatasetDateRefresh(event.dataset_date);
        }
      }
    },
    onReconcile: async () => {
      await reconcileLoadedAnnotationProjections();
    },
  });
  return null;
}

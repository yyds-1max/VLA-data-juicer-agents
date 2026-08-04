import { useEffect, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { useStore } from "zustand";

import { ConsoleSlidingTabs } from "../../components/console/ConsoleSlidingTabs";
import { annotationProjectionStore, loadAnnotationCapability } from "./projectionStore";

type WorkspaceTab = "jobs" | "reviews";

export function AnnotationWorkspaceLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const capability = useStore(annotationProjectionStore, (state) => state.capability);
  const capabilityLoaded = useStore(
    annotationProjectionStore,
    (state) => state.capabilityLoaded,
  );
  const [capabilityRequestFailed, setCapabilityRequestFailed] = useState(false);
  const active: WorkspaceTab = location.pathname.startsWith("/annotation/reviews")
    ? "reviews"
    : "jobs";
  const annotationWorkbench = /^\/annotation\/jobs\/[^/]+\/segments\/[^/]+\/?$/.test(
    location.pathname,
  );

  useEffect(() => {
    let activeRequest = true;
    void loadAnnotationCapability()
      .then(() => {
        if (activeRequest) setCapabilityRequestFailed(false);
      })
      .catch(() => {
        if (activeRequest) setCapabilityRequestFailed(true);
      });
    return () => {
      activeRequest = false;
    };
  }, []);

  const capabilityState = !capabilityLoaded
    ? { label: "正在检查处理环境", dot: "bg-[#9AA3B5]" }
    : capabilityRequestFailed
      ? { label: "处理环境状态未知", dot: "bg-[#9AA3B5]" }
      : capability?.available
        ? { label: "处理环境可用", dot: "bg-emerald-500" }
        : { label: "处理环境不可用", dot: "bg-amber-500" };

  return (
    <>
      {!annotationWorkbench ? <div className="relative z-0 isolate border-b border-console-line bg-white px-3 py-3 md:px-4 lg:px-5">
        <div className="mx-auto flex max-w-360 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <ConsoleSlidingTabs
            aria-label="自动标注页面"
            value={active}
            items={[
              { value: "jobs", label: "标注任务" },
              { value: "reviews", label: "人工复核" },
            ]}
            listClassName="sm:min-w-60"
            onValueChange={(value) => {
              navigate(value === "reviews" ? "/annotation/reviews" : "/annotation/jobs");
            }}
          />
          <div
            className="inline-flex min-h-8 items-center gap-2 self-end text-xs font-medium text-[#626B7D] sm:self-auto"
            role="status"
            aria-live="polite"
          >
            <span className={`size-2 rounded-full ${capabilityState.dot}`} aria-hidden="true" />
            {capabilityState.label}
          </div>
        </div>
      </div> : null}
      <Outlet />
    </>
  );
}

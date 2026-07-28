import { ClipboardCheck, Tags } from "lucide-react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";

import {
  Tabs,
  TabsList,
  TabsTrigger,
} from "../../components/ui/tabs";

type WorkspaceTab = "jobs" | "reviews";

export function AnnotationWorkspaceLayout() {
  const location = useLocation();
  const navigate = useNavigate();
  const active: WorkspaceTab = location.pathname.startsWith("/annotation/reviews")
    ? "reviews"
    : "jobs";

  return (
    <>
      <div className="border-b border-console-line bg-console-panel/90 px-3 md:px-4 lg:px-5">
        <div className="mx-auto max-w-360">
          <Tabs
            aria-label="自动标注页面"
            value={active}
            onValueChange={(value) => {
              navigate(value === "reviews" ? "/annotation/reviews" : "/annotation/jobs");
            }}
          >
            <TabsList variant="line" className="h-11 gap-4">
              <TabsTrigger
                value="jobs"
                className="h-10 flex-none gap-2 rounded-none px-1 text-console-muted data-active:text-console-text"
              >
                <Tags aria-hidden="true" className="h-4 w-4" />
                标注工作台
              </TabsTrigger>
              <TabsTrigger
                value="reviews"
                className="h-10 flex-none gap-2 rounded-none px-1 text-console-muted data-active:text-console-text"
              >
                <ClipboardCheck aria-hidden="true" className="h-4 w-4" />
                人工复核
              </TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
      </div>
      <Outlet />
    </>
  );
}

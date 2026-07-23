import { useState } from "react";
import { BrowserRouter, createBrowserRouter, RouterProvider } from "react-router-dom";

import { DataPilotButton } from "../components/datapilot/DataPilotButton";
import { DataPilotWindow } from "../components/datapilot/DataPilotWindow";
import { AppShell } from "./AppShell";

function AppContent() {
  return (
    <AppShell>
      <DataPilotButton />
      <DataPilotWindow />
    </AppShell>
  );
}

function DataRouterApp() {
  const [router] = useState(() => createBrowserRouter(
    [{
      path: "*",
      element: <AppContent />,
    }],
    { future: { v7_relativeSplatPath: true } },
  ));

  return <RouterProvider router={router} future={{ v7_startTransition: true }} />;
}

export function App({ routerMode = "data" }: { routerMode?: "data" | "declarative" }) {
  if (routerMode === "declarative") {
    return (
      <BrowserRouter future={{ v7_relativeSplatPath: true, v7_startTransition: true }}>
        <AppContent />
      </BrowserRouter>
    );
  }
  return <DataRouterApp />;
}

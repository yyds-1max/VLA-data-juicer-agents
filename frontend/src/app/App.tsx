import { DataPilotButton } from "../components/datapilot/DataPilotButton";
import { DataPilotWindow } from "../components/datapilot/DataPilotWindow";
import { AppShell } from "./AppShell";

export function App() {
  return (
    <AppShell>
      <DataPilotButton />
      <DataPilotWindow />
    </AppShell>
  );
}

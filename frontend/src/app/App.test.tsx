import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";

import { App } from "./App";

test("renders the DataPilot scaffold", () => {
  render(<App />);

  expect(screen.getByText("DataPilot")).toBeVisible();
});

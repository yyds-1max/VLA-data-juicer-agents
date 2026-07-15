import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import type { SessionRecord } from "../../api/types";
import { SessionHistoryPanel } from "./SessionHistoryPanel";

function historySessions(count: number): SessionRecord[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `session-${index}`,
    title: `History ${index}`,
    created_at: `2026-07-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
    updated_at: `2026-07-${String(index + 1).padStart(2, "0")}T00:00:00Z`,
  }));
}

test("the oldest session remains reachable for opening and deletion after pagination", () => {
  const sessions = historySessions(27);
  const onSelect = vi.fn();
  const onDelete = vi.fn();

  render(
    <SessionHistoryPanel
      sessions={sessions}
      onSelect={onSelect}
      onDelete={onDelete}
      onClose={vi.fn()}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "Open session History 26" }));
  fireEvent.click(screen.getByRole("button", { name: "Delete session History 26" }));

  expect(onSelect).toHaveBeenCalledWith(sessions[26]);
  expect(onDelete).toHaveBeenCalledWith(sessions[26]);
  expect(screen.getAllByRole("button", { name: /^Open session History / })).toHaveLength(27);
});

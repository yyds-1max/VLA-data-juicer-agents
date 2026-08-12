import "@testing-library/jest-dom/vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { getHistoricalVerifiedAsset } from "./api";
import { HistoricalVerifiedAssetPage } from "./HistoricalVerifiedAssetPage";

vi.mock("./api", () => ({
  getHistoricalVerifiedAsset: vi.fn(),
}));

const getAssetMock = vi.mocked(getHistoricalVerifiedAsset);

beforeEach(() => {
  vi.clearAllMocks();
  getAssetMock.mockResolvedValue({
    asset_ref: "verified_asset_0123456789abcdef0123456789abcdef",
    dataset_date: "20260623",
    source_clip: "20260623_145550",
    segment_ordinal: 2,
    segment_total: 6,
    content_sha256: "d".repeat(64),
    provenance: "historical_import",
    imported_at: "2026-08-09T00:00:00+00:00",
  });
});

test("renders only public historical verification provenance", async () => {
  render(
    <MemoryRouter
      initialEntries={[
        "/annotation/verified/verified_asset_0123456789abcdef0123456789abcdef",
      ]}
    >
      <Routes>
        <Route
          path="/annotation/verified/:assetRef"
          element={<HistoricalVerifiedAssetPage />}
        />
      </Routes>
    </MemoryRouter>,
  );

  expect(await screen.findByRole("heading", { name: "历史已验证版本" })).toBeVisible();
  expect(screen.getByText("20260623_145550")).toBeVisible();
  expect(screen.getByText("Segment 02 / 6")).toBeVisible();
  expect(screen.getByText("d".repeat(64))).toBeVisible();
  expect(document.body).not.toHaveTextContent("/media/");
  await waitFor(() => {
    expect(getAssetMock).toHaveBeenCalledWith(
      "verified_asset_0123456789abcdef0123456789abcdef",
    );
  });
});

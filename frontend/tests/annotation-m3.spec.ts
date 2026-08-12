import { expect, test } from "@playwright/test";

const ASSET_REF = "verified_asset_0123456789abcdef0123456789abcdef";

const lifecycleCounts = {
  total: 2,
  not_started: 1,
  processing: 0,
  waiting_initial_annotation: 0,
  annotated_pending_review: 0,
  verified: 1,
  returned: 0,
  discarded: 0,
  failed: 0,
};

const summary = {
  totals: {
    date_count: 1,
    clip_count: 2,
    total_duration_ns: 3_500_000_000,
    raw_message_count: 40,
    extracted_clip_count: 2,
    synced_clip_count: 1,
  },
  sync_distribution: { image: 3, pointcloud: 2, odom: 2, grid_map: 1 },
  annotation_totals: {
    annotated_clip_count: 1,
    verified_clip_count: 1,
    annotated_unit_count: 1,
    verified_unit_count: 1,
  },
  dates: [
    {
      date: "20270515",
      clip_count: 2,
      total_duration_ns: 3_500_000_000,
      raw_message_count: 40,
      extracted_clip_count: 2,
      synced_clip_count: 1,
      sync_frame_counts: { image: 3, pointcloud: 2, odom: 2, grid_map: 1 },
      status: "synced",
      annotation: {
        status: "partial",
        counts: lifecycleCounts,
        completed_unit_count: 1,
        annotated_unit_count: 1,
        verified_unit_count: 1,
        job_ref: null,
        review_ref: null,
        verified_review_ref: null,
        historical_asset_ref: ASSET_REF,
        updated_at: "2026-08-09T00:00:00Z",
        source: "historical_import",
      },
      clips: [
        {
          date: "20270515",
          clip: "clip_a",
          duration_ns: 1_500_000_000,
          raw_message_count: 18,
          topics: [],
          has_tmp_dir: true,
          has_sync_data: true,
          sequences: [],
          sync_frame_counts: { image: 3, pointcloud: 2, odom: 2, grid_map: 1 },
          status: "synced",
          errors: [],
          annotation: {
            status: "verified",
            counts: { ...lifecycleCounts, total: 1, not_started: 0 },
            completed_unit_count: 1,
            annotated_unit_count: 1,
            verified_unit_count: 1,
            job_ref: null,
            review_ref: null,
            verified_review_ref: null,
            historical_asset_ref: ASSET_REF,
            updated_at: "2026-08-09T00:00:00Z",
            source: "historical_import",
          },
        },
        {
          date: "20270515",
          clip: "clip_b",
          duration_ns: 2_000_000_000,
          raw_message_count: 22,
          topics: [],
          has_tmp_dir: true,
          has_sync_data: false,
          sequences: [],
          sync_frame_counts: { image: 0, pointcloud: 0, odom: 0, grid_map: 0 },
          status: "extracted",
          errors: [],
          annotation: {
            status: "not_started",
            counts: { ...lifecycleCounts, total: 0, verified: 0, not_started: 0 },
            completed_unit_count: 0,
            annotated_unit_count: 0,
            verified_unit_count: 0,
            job_ref: null,
            review_ref: null,
            verified_review_ref: null,
            historical_asset_ref: null,
            updated_at: null,
            source: "none",
          },
        },
      ],
    },
  ],
};

test.beforeEach(async ({ page }) => {
  await page.route("**/api/annotation/capabilities", async (route) => {
    await route.fulfill({ json: { available: true } });
  });
  await page.route("**/api/annotation/events/cursor", async (route) => {
    await route.fulfill({ json: { cursor: 0 } });
  });
  await page.route("**/api/annotation/events?after_seq=*", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: "retry: 60000\n\n",
    });
  });
});

test("dashboard refreshes the real annotation aggregate without changing routes", async ({ page }) => {
  let summaryRequests = 0;
  await page.route("**/api/navigation/datasets/summary", async (route) => {
    summaryRequests += 1;
    await route.fulfill({ json: summary });
  });

  await page.goto("/");
  await expect(page.getByText("自动标注覆盖率 100% · 1 Segments")).toBeVisible();
  await page.getByRole("button", { name: "刷新仪表盘数据" }).click();
  await expect.poll(() => summaryRequests).toBe(2);
  await expect(page).toHaveURL("/");
});

test("data management keeps both status axes and restores a historical deep link", async ({ page }) => {
  await page.route("**/api/navigation/datasets/summary", async (route) => {
    await route.fulfill({ json: summary });
  });
  await page.route(`**/api/annotation/verified-assets/${ASSET_REF}`, async (route) => {
    await route.fulfill({
      json: {
        asset_ref: ASSET_REF,
        dataset_date: "20270515",
        source_clip: "clip_a",
        segment_ordinal: 1,
        segment_total: 1,
        content_sha256: "a".repeat(64),
        provenance: "historical_import",
        imported_at: "2026-08-09T00:00:00Z",
      },
    });
  });

  await page.goto("/data");
  await expect(page.getByRole("columnheader", { name: "数据状态" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "标注状态" })).toBeVisible();
  await page.getByRole("combobox", { name: "标注状态筛选" }).click();
  await page.getByRole("option", { name: "已验证" }).click();
  await expect(page.getByText("20270515")).toBeVisible();
  await page.getByRole("button", { name: "查看已验证版本" }).click();
  await expect(page).toHaveURL(`/annotation/verified/${ASSET_REF}`);
  await expect(page.getByRole("heading", { name: "历史已验证版本" })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: "历史已验证版本" })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL("/data");
});

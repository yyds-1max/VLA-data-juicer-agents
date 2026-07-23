import { expect, test, type Locator, type Page, type Route } from "@playwright/test";

const JOB_REF = `job_${"a".repeat(32)}`;
const SEGMENT_REF = `segment_${"b".repeat(32)}`;
const TARGET_REF = `target_${"c".repeat(32)}`;
const DATASET_DATE = "20270605";
const SOURCE_CLIP = "20260605_160904";
const NOW = "2026-07-23T08:00:00.000Z";

type TestTarget = {
  target_ref: string;
  bbox: [number, number, number, number] | null;
  point: [number, number] | null;
  colors: {
    upper: string | null;
    lower: string | null;
    shoes: string | null;
  };
};

type DraftBody = {
  expected_segment_revision: number;
  expected_draft_revision: number | null;
  targets: TestTarget[];
};

type SegmentState = {
  segment_ref: string;
  ordinal: number;
  source_clip: string;
  status: "pending_initial_annotation" | "draft";
  state_revision: number;
  draft_revision: number | null;
  submitted_revision: number | null;
  first_frame: {
    url: string;
    width: number;
    height: number;
    sha256: string;
    etag: string;
  };
  draft: { revision: number; targets: TestTarget[] } | null;
  skip_reason: null;
};

type AnnotationMockOptions = {
  initialTargets?: TestTarget[];
  saveDelayMs?: number;
  conflictOnFirstSave?: boolean;
};

type AnnotationMock = {
  savedBodies: DraftBody[];
  currentSegment: () => SegmentState;
};

function cloneTargets(targets: TestTarget[]): TestTarget[] {
  return targets.map((target) => ({
    ...target,
    bbox: target.bbox ? [...target.bbox] : null,
    point: target.point ? [...target.point] : null,
    colors: { ...target.colors },
  }));
}

function completeTarget(x = 10): TestTarget {
  return {
    target_ref: TARGET_REF,
    bbox: [x, 10, 30, 30],
    point: [20, 20],
    colors: { upper: "green", lower: "gray", shoes: "white" },
  };
}

function emptyDatasetSummary() {
  return {
    totals: {
      date_count: 1,
      clip_count: 1,
      total_duration_ns: 1,
      raw_message_count: 1,
      extracted_clip_count: 1,
      synced_clip_count: 1,
    },
    sync_distribution: { image: 1, pointcloud: 1, odom: 1, grid_map: 0 },
    dates: [{
      date: DATASET_DATE,
      clip_count: 1,
      total_duration_ns: 1,
      raw_message_count: 1,
      extracted_clip_count: 1,
      synced_clip_count: 1,
      sync_frame_counts: { image: 1, pointcloud: 1, odom: 1, grid_map: 0 },
      status: "synced",
    }],
  };
}

async function installAnnotationMocks(
  page: Page,
  options: AnnotationMockOptions = {},
): Promise<AnnotationMock> {
  const initialTargets = cloneTargets(options.initialTargets ?? []);
  let segment: SegmentState = {
    segment_ref: SEGMENT_REF,
    ordinal: 1,
    source_clip: SOURCE_CLIP,
    status: initialTargets.length > 0 ? "draft" : "pending_initial_annotation",
    state_revision: initialTargets.length > 0 ? 2 : 1,
    draft_revision: initialTargets.length > 0 ? 1 : null,
    submitted_revision: null,
    first_frame: {
      url: `/api/annotation/jobs/${JOB_REF}/segments/${SEGMENT_REF}/first-frame`,
      width: 100,
      height: 80,
      sha256: "d".repeat(64),
      etag: `"${"d".repeat(64)}"`,
    },
    draft: initialTargets.length > 0
      ? { revision: 1, targets: initialTargets }
      : null,
    skip_reason: null,
  };
  const savedBodies: DraftBody[] = [];
  let saveCount = 0;

  const jobDetail = () => {
    const isDraft = segment.status === "draft";
    return {
      job_ref: JOB_REF,
      dataset_date: DATASET_DATE,
      source_clips: [SOURCE_CLIP],
      status: "waiting_initial_annotation",
      completion_outcome: null,
      state_revision: 2,
      calibration: {
        profile_ref: `calibration_${"e".repeat(32)}`,
        label: "20260529_go2w",
        content_sha256: "f".repeat(64),
      },
      counts: {
        total: 1,
        pending_initial_annotation: isDraft ? 0 : 1,
        draft: isDraft ? 1 : 0,
        submitted: 0,
        skipped: 0,
        tracking: 0,
        tracked: 0,
      },
      ready_for_tracking: false,
      ready_for_no_processable_targets: false,
      failure: null,
      created_at: NOW,
      updated_at: NOW,
      segments: [segment],
    };
  };

  await page.route("**/api/navigation/datasets/summary", async (route) => {
    await route.fulfill({ json: emptyDatasetSummary() });
  });
  await page.route(`**/api/navigation/datasets/${DATASET_DATE}`, async (route) => {
    await route.fulfill({
      json: {
        ...emptyDatasetSummary().dates[0],
        clips: [{
          date: DATASET_DATE,
          clip: SOURCE_CLIP,
          duration_ns: 1,
          raw_message_count: 1,
          topics: [],
          has_tmp_dir: true,
          has_sync_data: true,
          sequences: [],
          sync_frame_counts: { image: 1, pointcloud: 1, odom: 1, grid_map: 0 },
          status: "synced",
          errors: [],
        }],
      },
    });
  });

  await page.route("**/api/annotation/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const method = request.method();
    const jobPath = `/api/annotation/jobs/${JOB_REF}`;
    const segmentPath = `${jobPath}/segments/${SEGMENT_REF}`;

    if (pathname === "/api/annotation/capabilities" && method === "GET") {
      await route.fulfill({
        json: { available: true, runtime_id: "navigation_odom_v1", reason: null },
      });
      return;
    }
    if (pathname === "/api/annotation/calibration-profiles" && method === "GET") {
      await route.fulfill({ json: { profiles: [] } });
      return;
    }
    if (pathname === "/api/annotation/jobs" && method === "GET") {
      await route.fulfill({ json: { jobs: [jobDetail()] } });
      return;
    }
    if (pathname === jobPath && method === "GET") {
      await route.fulfill({ json: jobDetail() });
      return;
    }
    if (pathname === `${segmentPath}/first-frame` && method === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "image/svg+xml",
        headers: { ETag: segment.first_frame.etag },
        body: [
          '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="80" viewBox="0 0 100 80">',
          '<rect width="100" height="80" fill="#1e293b"/>',
          '<path d="M0 0L100 80M100 0L0 80" stroke="#64748b"/>',
          "</svg>",
        ].join(""),
      });
      return;
    }
    if (pathname === segmentPath && method === "GET") {
      await route.fulfill({ json: segment });
      return;
    }
    if (pathname === `${segmentPath}/draft` && method === "PUT") {
      const body = request.postDataJSON() as DraftBody;
      savedBodies.push({
        ...body,
        targets: cloneTargets(body.targets),
      });
      saveCount += 1;
      if (options.saveDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, options.saveDelayMs));
      }
      if (options.conflictOnFirstSave && saveCount === 1) {
        const serverTargets = cloneTargets(segment.draft?.targets ?? [completeTarget()]);
        if (serverTargets[0]?.bbox) serverTargets[0].bbox[0] = 22;
        const nextDraftRevision = (segment.draft_revision ?? 0) + 1;
        segment = {
          ...segment,
          status: "draft",
          state_revision: segment.state_revision + 1,
          draft_revision: nextDraftRevision,
          draft: { revision: nextDraftRevision, targets: serverTargets },
        };
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: "revision_conflict",
              message: "服务器上已有更新",
              current: { segment },
            },
          },
        });
        return;
      }
      const nextDraftRevision = (segment.draft_revision ?? 0) + 1;
      segment = {
        ...segment,
        status: "draft",
        state_revision: segment.state_revision + 1,
        draft_revision: nextDraftRevision,
        draft: {
          revision: nextDraftRevision,
          targets: cloneTargets(body.targets),
        },
      };
      await route.fulfill({ json: segment });
      return;
    }

    await unexpectedApi(route);
  });

  return {
    savedBodies,
    currentSegment: () => segment,
  };
}

async function unexpectedApi(route: Route) {
  await route.fulfill({
    status: 404,
    json: {
      detail: {
        code: "unexpected_test_request",
        message: `${route.request().method()} ${new URL(route.request().url()).pathname}`,
      },
    },
  });
}

async function waitForCanvas(page: Page): Promise<Locator> {
  const canvas = page.getByRole("application", { name: "首帧标注画布" });
  await expect(canvas).toBeVisible();
  await expect.poll(async () => (
    page.getByRole("img", { name: /resize 后首帧/ }).evaluate(
      (image: HTMLImageElement) => [image.naturalWidth, image.naturalHeight],
    )
  )).toEqual([100, 80]);
  return canvas;
}

async function canvasCoordinate(canvas: Locator, x: number, y: number) {
  await canvas.scrollIntoViewIfNeeded();
  const bounds = await canvas.boundingBox();
  if (!bounds) throw new Error("annotation canvas has no layout box");
  return {
    x: bounds.x + (x / 100) * bounds.width,
    y: bounds.y + (y / 80) * bounds.height,
  };
}

async function dragOnCanvas(
  page: Page,
  canvas: Locator,
  from: [number, number],
  to: [number, number],
) {
  const start = await canvasCoordinate(canvas, from[0], from[1]);
  const end = await canvasCoordinate(canvas, to[0], to[1]);
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  await page.mouse.move(end.x, end.y);
  await page.mouse.up();
}

async function dragElementToCanvas(
  page: Page,
  element: Locator,
  canvas: Locator,
  to: [number, number],
) {
  await element.scrollIntoViewIfNeeded();
  const elementBounds = await element.boundingBox();
  if (!elementBounds) throw new Error("annotation handle has no layout box");
  const end = await canvasCoordinate(canvas, to[0], to[1]);
  await page.mouse.move(
    elementBounds.x + elementBounds.width / 2,
    elementBounds.y + elementBounds.height / 2,
  );
  await page.mouse.down();
  await page.mouse.move(end.x, end.y);
  await page.mouse.up();
}

test("restores a deep link and persists drawing, moving, resizing, point editing, zoom and pan", async ({ page }) => {
  const mock = await installAnnotationMocks(page);
  const segmentUrl = `/annotation/jobs/${JOB_REF}/segments/${SEGMENT_REF}`;

  await page.goto(segmentUrl);
  await waitForCanvas(page);
  await page.reload();
  const canvas = await waitForCanvas(page);

  await page.getByRole("button", { name: "框选目标" }).click();
  await dragOnCanvas(page, canvas, [10, 10], [50, 50]);
  const foregroundPoint = await canvasCoordinate(canvas, 20, 20);
  await page.mouse.click(foregroundPoint.x, foregroundPoint.y);
  await page.getByLabel("master 上衣颜色").selectOption("green");
  await page.getByLabel("master 裤子颜色").selectOption("gray");
  await page.getByLabel("master 鞋子颜色").selectOption("white");
  await expect.poll(() => mock.savedBodies.length).toBeGreaterThan(0);

  await page.getByRole("button", { name: "选择/调整" }).click();
  await dragOnCanvas(page, canvas, [40, 40], [45, 45]);
  await dragElementToCanvas(
    page,
    canvas.locator('[data-resize-direction="se"]'),
    canvas,
    [65, 65],
  );
  await dragElementToCanvas(
    page,
    canvas.locator('[data-resize-direction="w"]'),
    canvas,
    [25, 40],
  );
  await dragElementToCanvas(
    page,
    canvas.locator("[data-annotation-point-ref]"),
    canvas,
    [35, 30],
  );

  await expect.poll(() => {
    const latest = mock.savedBodies.at(-1)?.targets[0];
    return latest ? JSON.stringify({ bbox: latest.bbox, point: latest.point }) : "";
  }).toBe(JSON.stringify({ bbox: [25, 15, 40, 50], point: [35, 30] }));

  await page.getByRole("button", { name: "放大画布" }).click();
  await page.getByRole("button", { name: "放大画布" }).click();
  await expect(page.getByText("150%", { exact: true })).toBeVisible();
  const scrollArea = page.locator("[data-annotation-canvas-scroll]");
  await expect.poll(() => scrollArea.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await scrollArea.evaluate((element) => {
    element.scrollLeft = element.scrollWidth;
  });
  await expect.poll(() => scrollArea.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);

  await page.reload();
  await waitForCanvas(page);
  await expect(page.getByLabel("master bbox x")).toHaveValue("25");
  await expect(page.getByLabel("master bbox y")).toHaveValue("15");
  await expect(page.getByLabel("master bbox w")).toHaveValue("40");
  await expect(page.getByLabel("master bbox h")).toHaveValue("50");
  await expect(page.getByLabel("master point x")).toHaveValue("35");
  await expect(page.getByLabel("master point y")).toHaveValue("30");
});

test("browser back and forward wait for the current dirty draft to persist", async ({ page }) => {
  const mock = await installAnnotationMocks(page, {
    initialTargets: [completeTarget()],
    saveDelayMs: 600,
  });
  const jobUrl = `/annotation/jobs/${JOB_REF}`;
  const segmentUrl = `${jobUrl}/segments/${SEGMENT_REF}`;

  await page.goto(jobUrl);
  await page.getByRole("button", { name: /Segment 01/ }).click();
  await waitForCanvas(page);
  await page.getByLabel("master bbox x").fill("11");

  const backNavigation = page.goBack();
  await expect.poll(() => mock.savedBodies.length).toBe(1);
  await page.waitForTimeout(100);
  await expect(page).toHaveURL(segmentUrl);
  await backNavigation;
  await expect(page).toHaveURL(jobUrl);

  await page.goForward();
  await waitForCanvas(page);
  await expect(page.getByLabel("master bbox x")).toHaveValue("11");
  await page.getByRole("button", { name: "数据管理" }).click();
  await expect(page).toHaveURL("/data");
  await expect(page.getByRole("heading", { name: "数据管理" })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(segmentUrl);
  await waitForCanvas(page);
  await page.getByLabel("master bbox x").fill("12");

  const forwardNavigation = page.goForward();
  await expect.poll(() => mock.savedBodies.length).toBe(2);
  await page.waitForTimeout(100);
  await expect(page).toHaveURL(segmentUrl);
  await forwardNavigation;
  await expect(page).toHaveURL("/data");
  expect(mock.savedBodies[1]).toMatchObject({
    expected_segment_revision: 3,
    expected_draft_revision: 2,
  });
  expect(mock.savedBodies[1].targets[0].bbox).toEqual([12, 10, 30, 30]);
});

test("a 409 while flushing browser history keeps the user on the segment", async ({ page }) => {
  await installAnnotationMocks(page, {
    initialTargets: [completeTarget()],
    conflictOnFirstSave: true,
  });
  const jobUrl = `/annotation/jobs/${JOB_REF}`;
  const segmentUrl = `${jobUrl}/segments/${SEGMENT_REF}`;

  await page.goto(jobUrl);
  await page.getByRole("button", { name: /Segment 01/ }).click();
  await waitForCanvas(page);
  await page.getByLabel("master bbox x").fill("11");

  await page.goBack();
  await expect(page).toHaveURL(segmentUrl);
  await expect(page.getByText("检测到并发修改")).toBeVisible();
  await page.getByRole("button", { name: "使用服务器版本" }).click();
  await expect(page.getByLabel("master bbox x")).toHaveValue("22");
  await expect(page).toHaveURL(segmentUrl);
});

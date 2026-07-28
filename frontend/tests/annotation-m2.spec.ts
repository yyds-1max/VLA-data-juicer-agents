import { expect, test, type Page, type Route } from "@playwright/test";

const REVIEW_ONE = `review_${"1".repeat(32)}`;
const REVIEW_TWO = `review_${"2".repeat(32)}`;
const JOB_REF = `job_${"3".repeat(32)}`;
const SEGMENT_ONE = `segment_${"4".repeat(32)}`;
const SEGMENT_TWO = `segment_${"5".repeat(32)}`;
const TRAJECTORY_ONE = `trajectory_revision_${"6".repeat(32)}`;
const TRAJECTORY_TWO = `trajectory_revision_${"7".repeat(32)}`;
const TARGET_REF = `target_${"8".repeat(32)}`;
const DATASET_DATE = "20270623";
const SOURCE_CLIP = "20260623_145550";

type ReviewStatus = "pending" | "approved";

function reviewFixture({
  reviewRef,
  segmentRef,
  trajectoryRef,
  ordinal,
  status,
}: {
  reviewRef: string;
  segmentRef: string;
  trajectoryRef: string;
  ordinal: number;
  status: ReviewStatus;
}) {
  const published = status === "approved";
  return {
    review_ref: reviewRef,
    status,
    state_revision: published ? 3 : 1,
    job_ref: JOB_REF,
    dataset_date: DATASET_DATE,
    source_clip: SOURCE_CLIP,
    segment_ref: segmentRef,
    segment_ordinal: ordinal,
    trajectory_revision: {
      revision_ref: trajectoryRef,
      content_sha256: String(ordinal).repeat(64),
    },
    processing_calibration: {
      profile_ref: `calibration_${"9".repeat(32)}`,
      label: "20260529_go2w",
      content_sha256: "a".repeat(64),
    },
    fix_draft: null,
    fix_revisions: [],
    active_fix_run: null,
    fix_failure: null,
    latest_publication: published
      ? {
          fix_revision_ref: `fix_revision_${"b".repeat(32)}`,
          attempt: 1,
          status: "published",
          content_sha256: "c".repeat(64),
          failure: null,
          created_at: "2026-07-28T08:00:00Z",
        }
      : null,
    created_at: "2026-07-28T08:00:00Z",
    updated_at: `2026-07-28T08:00:0${ordinal}Z`,
  };
}

const reviews = [
  reviewFixture({
    reviewRef: REVIEW_ONE,
    segmentRef: SEGMENT_ONE,
    trajectoryRef: TRAJECTORY_ONE,
    ordinal: 1,
    status: "pending",
  }),
  reviewFixture({
    reviewRef: REVIEW_TWO,
    segmentRef: SEGMENT_TWO,
    trajectoryRef: TRAJECTORY_TWO,
    ordinal: 2,
    status: "approved",
  }),
];

function evidenceFor(reviewRef: string) {
  const owner = reviews.find((review) => review.review_ref === reviewRef);
  if (!owner) throw new Error("unknown review fixture");
  return {
    availability: "available",
    review_ref: owner.review_ref,
    trajectory_revision_ref: owner.trajectory_revision.revision_ref,
    review_state_revision: owner.state_revision,
    draft_revision: null,
    frame_count: 1,
    frames: [{
      frame_index: 0,
      pass: false,
      camera: null,
      gridmap: null,
      targets: [{
        target_ref: TARGET_REF,
        label: "Master",
        position: [1, 2],
        direction: 0,
        speed: 1,
        color: ["black", "black", "black"],
        image_box: null,
        trajectory_points: [[1, 2]],
      }],
    }],
    draft_commands: [],
  };
}

async function unexpectedApi(route: Route) {
  const request = route.request();
  await route.fulfill({
    status: 404,
    json: {
      detail: {
        code: "unexpected_test_request",
        message: `${request.method()} ${new URL(request.url()).pathname}`,
      },
    },
  });
}

async function installReviewMocks(page: Page) {
  await page.route("**/api/annotation/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname } = url;

    if (
      pathname === "/api/annotation/calibration-profiles"
      && request.method() === "GET"
    ) {
      await route.fulfill({ json: { profiles: [] } });
      return;
    }
    if (pathname === "/api/annotation/reviews" && request.method() === "GET") {
      await route.fulfill({ json: { reviews } });
      return;
    }
    const reviewMatch = pathname.match(
      /^\/api\/annotation\/reviews\/(review_[0-9a-f]{32})$/,
    );
    if (reviewMatch && request.method() === "GET") {
      const review = reviews.find((item) => item.review_ref === reviewMatch[1]);
      if (review) {
        await route.fulfill({ json: review });
        return;
      }
    }
    const evidenceMatch = pathname.match(
      /^\/api\/annotation\/reviews\/(review_[0-9a-f]{32})\/evidence\/trajectory$/,
    );
    if (evidenceMatch && request.method() === "GET") {
      await route.fulfill({ json: evidenceFor(evidenceMatch[1]) });
      return;
    }
    await unexpectedApi(route);
  });
}

test("review deep links survive refresh and preserve anonymous Segment browser history", async ({ page }) => {
  await installReviewMocks(page);

  await page.goto("/annotation/reviews");
  await page.reload();
  await expect(page.getByRole("heading", { name: "人工复核" })).toBeVisible();
  await page.getByLabel("复核状态筛选").click();
  await page.getByRole("option", { name: "全部状态" }).click();
  await expect(page.getByText("2 个复核单元")).toBeVisible();

  await page.getByRole("button", { name: "进入人工 Fix" }).click();
  await expect(page).toHaveURL(`/annotation/reviews/${REVIEW_ONE}`);
  await expect(page.getByRole("button", { name: "当前 Segment 01" })).toBeVisible();
  await expect(page.getByRole("button", { name: "切换到 Segment 02" })).toContainText(
    "已验证",
  );
  await expect(page.locator("body")).not.toContainText(SEGMENT_ONE);
  await expect(page.locator("body")).not.toContainText(SEGMENT_TWO);

  await page.reload();
  await expect(page.getByRole("button", { name: "当前 Segment 01" })).toBeVisible();
  await page.getByRole("button", { name: "切换到 Segment 02" }).click();
  await expect(page).toHaveURL(`/annotation/reviews/${REVIEW_TWO}`);
  await expect(page.getByRole("button", { name: "当前 Segment 02" })).toBeVisible();

  await page.goBack();
  await expect(page).toHaveURL(`/annotation/reviews/${REVIEW_ONE}`);
  await expect(page.getByRole("button", { name: "当前 Segment 01" })).toBeVisible();

  await page.goForward();
  await expect(page).toHaveURL(`/annotation/reviews/${REVIEW_TWO}`);
  await expect(page.getByRole("button", { name: "当前 Segment 02" })).toBeVisible();
});

test.describe("mobile review smoke", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("opens a terminal review deep link with the Segment queue intact", async ({ page }) => {
    await installReviewMocks(page);

    await page.goto(`/annotation/reviews/${REVIEW_TWO}`);
    await expect(page.getByRole("button", { name: "当前 Segment 02" })).toBeVisible();
    await expect(page.getByRole("button", { name: "切换到 Segment 01" })).toBeVisible();
    await expect(page.getByText("已验证", { exact: true }).first()).toBeVisible();
  });
});

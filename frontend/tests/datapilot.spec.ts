import { expect, test } from "@playwright/test";

test("opens the DataPilot draft panel from the floating button", async ({ page }) => {
  await page.goto("/");

  await page.getByRole("button", { name: "Open DataPilot" }).click();

  await expect(page.getByText("开始一个任务")).toBeVisible();
  await expect(page.getByPlaceholder("我们要做什么？")).toBeVisible();
  await expect(page.getByText("继续任务")).toHaveCount(0);
});

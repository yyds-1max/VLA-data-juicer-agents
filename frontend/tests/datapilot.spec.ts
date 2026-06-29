import { expect, test } from "@playwright/test";

test("preserves the DataPilot floating entry over the migrated console", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByText("智瀚星途 DataLoop")).toBeVisible();
  await expect(page.getByRole("heading", { name: "闭环仪表盘" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open DataPilot" })).toBeVisible();

  await page.getByRole("button", { name: "Open DataPilot" }).click();

  await expect(page.getByText("开始一个任务")).toBeVisible();
  await expect(page.getByPlaceholder("我们要做什么？")).toBeVisible();
  await expect(page.getByText("继续任务")).toHaveCount(0);

  const dialog = page.getByRole("dialog", { name: "DataPilot" });
  await page.evaluate(() => window.scrollTo(0, 0));
  await dialog.hover();
  await page.mouse.wheel(0, 500);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);

  await page.mouse.move(320, 720);
  await page.mouse.wheel(0, 500);
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Close DataPilot" }).click();
  await expect(page.getByRole("dialog", { name: "DataPilot" })).toHaveCount(0);

  await page.getByRole("button", { name: "测试/仿真" }).click();
  await expect(page.getByRole("heading", { name: "测试/仿真" })).toBeVisible();
  await expect(page.getByText("仿真场景配置")).toBeVisible();
});

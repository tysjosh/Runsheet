/**
 * Settings → System health is platform_admin only (task 3.9, D12). Analytics
 * has no Ops Monitoring tab for anyone, platform_admin included.
 */
import { expect, test } from "@playwright/test";
import { installShellFake, signIn } from "./shellFake";

test("platform_admin sees System health with poison-queue figures", async ({
  page,
  context,
}) => {
  await signIn(context, ["admin", "platform_admin"]);
  await installShellFake(page);
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/dashboard/settings?tab=system");
  await expect(page.locator("h1")).toHaveText("Settings");
  const nav = page.getByRole("navigation", { name: "Settings sections" });
  await expect(
    nav.getByRole("button", { name: "System health" }),
  ).toBeVisible();
  await expect(page.getByText("Queue depth")).toBeVisible();
  await expect(page.getByText("Healthy")).toBeVisible();
});

for (const roles of [["admin"], ["dispatcher"]]) {
  test(`${roles.join("+")} does not see System health`, async ({
    page,
    context,
  }) => {
    await signIn(context, roles);
    await installShellFake(page);
    await page.goto("/dashboard/settings?tab=system");
    await expect(page.locator("h1")).toHaveText("Settings");
    const nav = page.getByRole("navigation", { name: "Settings sections" });
    await expect(nav.getByRole("button").first()).toBeVisible();
    await expect(
      nav.getByRole("button", { name: "System health" }),
    ).toHaveCount(0);
    await expect(page.getByText("Queue depth")).toHaveCount(0);
  });
}

test("Analytics has no Ops Monitoring tab for platform_admin", async ({
  page,
  context,
}) => {
  await signIn(context, ["admin", "platform_admin"]);
  await installShellFake(page);
  await page.goto("/dashboard/analytics");
  await expect(page.getByRole("tab", { name: /Overview/ })).toBeVisible();
  await expect(page.getByRole("tab", { name: /Ops monitoring/i })).toHaveCount(
    0,
  );
});

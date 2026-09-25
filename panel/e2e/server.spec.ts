/**
 * Server against the real backend: the health verdict, system info, disks, network, top
 * processes, and the resource monitor card - including acknowledging an open finding, which
 * must also clear it from the overview's Needs attention.
 */

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

test("shows the health verdict, system info and network, and passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/server");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("Server");

  await expect(page.getByRole("heading", { name: "Health" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "System" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Network" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Top processes" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Network interfaces" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Process" })).toBeVisible();

  await settle(page);
  await expectNoA11yViolations(page, "the server page");
});

test("sorts top processes without reloading the page", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/server");
  const section = page.locator("section", { has: page.getByRole("heading", { name: "Top processes" }) });
  const table = section.getByRole("region");
  await expect(table.getByRole("row").filter({ hasNot: page.getByRole("columnheader") }).first()).toBeVisible();

  const sorted = page.waitForResponse((response) => response.url().includes("/api/system/processes") && response.url().includes("sort_by=memory"));
  await page.getByRole("combobox", { name: "Sort processes by" }).click();
  await page.getByRole("option", { name: "By memory" }).click();
  expect((await sorted).status()).toBe(200);
});

test("the resource monitor shows its unit and open findings", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/server");
  const monitor = page.getByRole("region", { name: "Resource monitor" });
  await expect(monitor).toBeVisible();
  await expect(monitor.getByText("Running")).toBeVisible();
  await expect(monitor.getByText("xmrig")).toBeVisible();
  await expect(monitor.getByText("node")).toBeVisible();
});

test("acknowledging an open finding removes it here and from the overview's Needs attention", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/");
  const attention = page.getByRole("region", { name: /Needs attention/ });
  await expect(attention.getByText("xmrig")).toBeVisible();

  await page.goto("/server");
  const monitor = page.getByRole("region", { name: "Resource monitor" });
  await expect(monitor.getByText("xmrig")).toBeVisible();

  const acknowledged = page.waitForResponse((response) => /\/api\/monitor\/observations\/\d+\/acknowledge$/.test(response.url()));
  await monitor.getByRole("button", { name: "Acknowledge finding about xmrig" }).click();
  expect((await acknowledged).status()).toBe(200);
  await expect(monitor.getByText("xmrig")).toBeHidden();

  await page.goto("/");
  await expect(page.getByRole("region", { name: /Needs attention/ }).getByText("xmrig")).toBeHidden();
});

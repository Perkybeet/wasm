/**
 * The charts' reading and enlarging against the real backend: hovering reads one sample out in
 * the legend row, the arrow keys step through them and say each one politely, and Expand opens
 * the chart large with the page's range, drag-to-zoom and the table. The overview's machine
 * charts are fed by the live collector; an app's charts by thirty days of seeded history.
 * Both themes, with axe in the dialog and the CSP and console gates of every test.
 */

import type { Locator, Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

const DOMAIN = "tienda.cittek.es";
const CLOCK = /^\d{2}:\d{2}$/;

/** The overview's CPU chart once the collector has at least two samples to draw. */
async function overviewCpuChart(page: Page): Promise<Locator> {
  const figure = page.getByRole("region", { name: "Machine", exact: true }).locator("figure").filter({ hasText: "CPU" });
  // A fresh server may still be "collecting samples"; the collector adds one every few seconds.
  await expect(async () => {
    if (!(await figure.getByRole("img").isVisible())) await page.reload();
    await expect(figure.getByRole("img")).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 45_000 });
  return figure;
}

function readoutTime(scope: Locator): Locator {
  return scope.locator("[data-readout-time]");
}

/** Moves the pointer over a fraction of the plot's width, halfway down. */
async function pointAt(page: Page, plot: Locator, fraction: number): Promise<void> {
  await plot.scrollIntoViewIfNeeded();
  const box = await plot.boundingBox();
  if (box === null) throw new Error("the plot is not on screen");
  await page.mouse.move(box.x + box.width * fraction, box.y + box.height / 2, { steps: 4 });
}

test("hovering an overview chart reads the sample under the pointer out in its legend", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer);
  const figure = await overviewCpuChart(page);
  const time = readoutTime(figure);
  await expect(time).toHaveText("Latest");
  const latest = await figure.getByRole("list", { name: "Series" }).textContent();

  const plot = figure.getByRole("img");
  await pointAt(page, plot, 0.3);
  // The hovered sample's local clock on screen, the full moment for a screen reader.
  await expect(time.locator('[aria-hidden="true"]')).toHaveText(CLOCK);
  await expect(time.locator("time")).toHaveAttribute("datetime", /^\d{4}-\d{2}-\d{2}T/);
  await expect(figure.getByRole("list", { name: "Series" })).toHaveText(/^CPU\s*\d+(\.\d+)?%$/);

  // Off the chart: back to the newest values.
  await page.mouse.move(5, 5);
  await expect(time).toHaveText("Latest");
  await expect(figure.getByRole("list", { name: "Series" })).toHaveText(latest ?? "");
});

test("the arrow keys step through an overview chart's samples and say each one", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer);
  const figure = await overviewCpuChart(page);
  const chart = figure.getByRole("application", { name: "CPU" });
  await chart.focus();
  await page.keyboard.press("Home");
  const first = await readoutTime(figure).locator('[aria-hidden="true"]').textContent();
  expect(first).toMatch(CLOCK);
  await expect(figure.getByRole("status")).toHaveText(new RegExp(`^${first ?? ""}, CPU \\d+(\\.\\d+)?%$`));
  await page.keyboard.press("End");
  await page.keyboard.press("ArrowLeft");
  await expect(readoutTime(figure).locator('[aria-hidden="true"]')).toHaveText(CLOCK);
  await page.keyboard.press("Escape");
  await expect(readoutTime(figure)).toHaveText("Latest");
  await settle(page);
  await expectNoA11yViolations(page, "the overview with a chart focused");
});

test("Expand opens an overview chart large, with the page's range, and passes axe", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer);
  const figure = await overviewCpuChart(page);
  await figure.getByRole("button", { name: "Expand CPU" }).click();
  const dialog = page.getByRole("dialog", { name: "CPU" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("img", { name: /^CPU, last hour/ })).toBeVisible();
  const plotBox = await dialog.getByRole("img").boundingBox();
  expect(plotBox?.height ?? 0).toBeGreaterThanOrEqual(240);
  await settle(page);
  await expectNoA11yViolations(page, "an enlarged overview chart");

  // The page's range, from inside the dialog: the URL follows and the dialog stays.
  await dialog.getByRole("radio", { name: "24h" }).click();
  await expect(page).toHaveURL(/\/\?window=24h$/);
  await expect(dialog.getByRole("img", { name: /^CPU, last 24 hours/ })).toBeVisible();

  await dialog.getByRole("button", { name: "View as table" }).click();
  await expect(dialog.getByRole("region", { name: "CPU data" })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "an enlarged overview chart as a table");

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(figure.getByRole("button", { name: "Expand CPU" })).toBeFocused();
});

test("an enlarged app chart zooms to a dragged stretch and resets", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}/metrics`);
  const figure = page.locator("figure").filter({ hasText: "CPU" }).first();
  await expect(figure.getByRole("img", { name: /^CPU, last 24 hours/ })).toBeVisible();
  await figure.getByRole("button", { name: "Expand CPU" }).click();
  const dialog = page.getByRole("dialog", { name: "CPU" });
  const plot = dialog.getByRole("img", { name: /^CPU, last 24 hours/ });
  await expect(plot).toBeVisible();
  await settle(page);

  const reset = dialog.getByRole("button", { name: "Reset zoom" });
  await expect(reset).toBeDisabled();
  const box = await plot.boundingBox();
  if (box === null) throw new Error("the plot is not on screen");
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * 0.4, y);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.7, y, { steps: 8 });
  await page.mouse.up();
  await expect(reset).toBeEnabled();
  await expect(dialog.getByText(/^Showing \d{2}:\d{2} to \d{2}:\d{2}\./)).toBeVisible();
  // The table follows the zoom: fewer rows than the whole day's.
  await dialog.getByRole("button", { name: "View as table" }).click();
  const zoomedRows = await dialog.getByRole("region", { name: "CPU data" }).getByRole("row").count();
  await dialog.getByRole("button", { name: "View as table" }).click();

  await reset.click();
  await expect(reset).toBeDisabled();
  await dialog.getByRole("button", { name: "View as table" }).click();
  const allRows = await dialog.getByRole("region", { name: "CPU data" }).getByRole("row").count();
  expect(zoomedRows).toBeLessThan(allRows);
  await dialog.getByRole("button", { name: "View as table" }).click();

  // Zooming without dragging, for anyone who cannot drag.
  await dialog.getByRole("button", { name: "Zoom in" }).click();
  await expect(reset).toBeEnabled();
  await dialog.getByRole("button", { name: "Zoom out" }).click();
  await expect(reset).toBeDisabled();

  // A new range starts whole.
  await dialog.getByRole("button", { name: "Zoom in" }).click();
  await dialog.getByRole("radio", { name: "7d" }).click();
  await expect(page).toHaveURL(/[?&]range=7d/);
  await expect(dialog.getByRole("img", { name: /^CPU, last 7 days/ })).toBeVisible();
  await expect(reset).toBeDisabled();
  await settle(page);
  await expectNoA11yViolations(page, "an enlarged app chart");
});

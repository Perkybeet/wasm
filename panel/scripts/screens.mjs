// Captures the design gallery for visual review: both themes, desktop and phone widths,
// every section on its own, and the overlays open.
//
//   npm run dev -- --port 5199 --strictPort &
//   node scripts/screens.mjs [--url http://127.0.0.1:5199/__design] [--out /tmp/console-design]
//
// Development aid only: it drives the dev server, never a build, and writes outside the repo.

import { mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";

import { chromium } from "@playwright/test";

const { values } = parseArgs({
  options: {
    url: { type: "string", default: "http://127.0.0.1:5199/__design" },
    out: { type: "string", default: "/tmp/console-design" },
    only: { type: "string" },
  },
});

const THEMES = /** @type {const} */ (["light", "dark"]);
const WIDTHS = [1440, 390];

/**
 * @param {import("@playwright/test").Page} page
 * @param {string} testId
 */
async function open(page, testId) {
  const trigger = page.getByTestId(testId);
  await trigger.scrollIntoViewIfNeeded();
  await trigger.click();
  await page.waitForTimeout(350);
}

/** @param {import("@playwright/test").Page} page */
async function closeAll(page) {
  await page.keyboard.press("Escape");
  await page.waitForTimeout(250);
}

async function main() {
  const out = values.out;
  await rm(out, { recursive: true, force: true });
  const browser = await chromium.launch();
  /** @type {string[]} */
  const problems = [];

  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      const dir = path.join(out, `${theme}-${String(width)}`);
      await mkdir(dir, { recursive: true });
      const context = await browser.newContext({
        viewport: { width, height: width > 600 ? 1600 : 844 },
        colorScheme: theme,
        deviceScaleFactor: width > 600 ? 1 : 2,
        reducedMotion: "reduce",
      });
      const page = await context.newPage();
      page.on("console", (message) => {
        if (message.type() === "error" || message.type() === "warning") {
          problems.push(`[${theme} ${String(width)}] ${message.type()}: ${message.text()}`);
        }
      });
      page.on("pageerror", (error) => problems.push(`[${theme} ${String(width)}] pageerror: ${error.message}`));

      await page.goto(values.url, { waitUntil: "networkidle" });
      await page.evaluate(() => document.fonts.ready);
      await page.waitForTimeout(400);

      const sections = await page.locator("[data-gallery-section]").evaluateAll((nodes) =>
        nodes.map((node) => node.getAttribute("data-gallery-section") ?? ""),
      );
      // The sticky header would sit on top of whichever section is being captured.
      const header = page.locator("body header").first();
      await header.evaluate((node) => {
        node.style.visibility = "hidden";
      });
      let index = 0;
      for (const id of sections) {
        index += 1;
        if (values.only && !values.only.split(",").includes(id)) continue;
        const section = page.locator(`[data-gallery-section="${id}"]`);
        await section.scrollIntoViewIfNeeded();
        await page.waitForTimeout(120);
        await section.screenshot({ path: path.join(dir, `${String(index).padStart(2, "0")}-${id}.png`) });
      }

      await header.evaluate((node) => {
        node.style.visibility = "";
      });

      if (!values.only) {
        await page.evaluate(() => {
          window.scrollTo(0, 0);
        });
        await page.screenshot({ path: path.join(dir, "00-top.png") });

        /** @type {[string, string][]} */
        const overlays = [
          ["open-dialog", "dialog"],
          ["open-confirm", "confirm"],
          ["open-drawer", "drawer"],
          ["open-menu", "menu"],
          ["open-popover", "popover"],
        ];
        for (const [testId, name] of overlays) {
          await open(page, testId);
          if (name === "confirm") await page.keyboard.type("shop.arenna");
          await page.screenshot({ path: path.join(dir, `overlay-${name}.png`) });
          await closeAll(page);
        }

        const runtime = page.getByRole("combobox").first();
        await runtime.scrollIntoViewIfNeeded();
        await runtime.click();
        await page.waitForTimeout(300);
        await page.screenshot({ path: path.join(dir, "overlay-select.png") });
        await closeAll(page);

        await page.getByTestId("toast-success").scrollIntoViewIfNeeded();
        await page.getByTestId("toast-success").click();
        await page.getByTestId("toast-error").click();
        await page.waitForTimeout(400);
        await page.screenshot({ path: path.join(dir, "overlay-toasts.png") });
        await page.locator(".toast").first().hover();
        await page.waitForTimeout(400);
        await page.screenshot({ path: path.join(dir, "overlay-toasts-expanded.png") });

        const search = page.getByRole("searchbox", { name: "Search output" }).first();
        await search.scrollIntoViewIfNeeded();
        await search.fill("npm");
        await page.waitForTimeout(200);
        await page.locator('[data-gallery-section="logs"]').screenshot({ path: path.join(dir, "logs-search.png") });

        const deploy = page.getByRole("button", { name: "Deploy", exact: true });
        await deploy.scrollIntoViewIfNeeded();
        await page.getByRole("button", { name: "Restart" }).first().focus();
        await page.keyboard.press("Shift+Tab");
        await page.waitForTimeout(150);
        await page.locator('[data-gallery-section="button"]').screenshot({ path: path.join(dir, "focus-ring.png") });

        await page.getByRole("button", { name: "Search" }).first().focus();
        await page.getByRole("button", { name: "Search" }).first().hover();
        await page.waitForTimeout(900);
        await page.screenshot({ path: path.join(dir, "overlay-tooltip.png") });
      }
      await context.close();
    }
  }
  await browser.close();
  if (problems.length > 0) {
    console.log(`Console problems:\n${problems.join("\n")}`);
  }
  console.log(`Screenshots in ${out}`);
}

await main();

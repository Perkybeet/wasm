/**
 * Screenshots of every page for a person (or a model) to review: both themes (the light and
 * dark projects), a 1440px desktop, a 1920px wide screen and a 390px phone. Tagged @screens
 * and left out of the default run; `npm run e2e:screens` writes them to e2e/__screens__/<theme>/, or to
 * $WASM_SCREENS_DIR/<theme>/ when set (a review pass keeps its captures out of the tree).
 *
 * The pages still pass the CSP and console gates of every test, so a screenshot is never of
 * a page that is quietly broken.
 */

import type { Page } from "@playwright/test";
import path from "node:path";

import { expect, settle, signIn, test } from "./fixtures";
import { ROUTES, routePath } from "./routes";

const OUT = process.env.WASM_SCREENS_DIR ?? path.join(import.meta.dirname, "__screens__");

const DESKTOP = { width: 1440, height: 900 };
/** A 1080p monitor: where a cap too narrow wastes the screen and one too wide stretches forms. */
const WIDE = { width: 1920, height: 1080 };
const PHONE = { width: 390, height: 844 };

/** Every size a page is captured at, in order; the name is the file's suffix. */
const SIZES: readonly { suffix: string; size: { width: number; height: number } }[] = [
  { suffix: "desktop", size: DESKTOP },
  { suffix: "wide", size: WIDE },
  { suffix: "mobile", size: PHONE },
];

async function captureSizes(page: Page, dir: string, name: string): Promise<void> {
  for (const { suffix, size } of SIZES) {
    await page.setViewportSize(size);
    await settle(page);
    await page.screenshot({ path: path.join(dir, `${name}-${suffix}.png`), fullPage: true });
  }
}

test.describe("screens @screens", () => {
  test("sign-in", async ({ page }, testInfo) => {
    const dir = path.join(OUT, testInfo.project.name);
    await page.setViewportSize(DESKTOP);
    await page.goto("/login");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("Sign in");
    await captureSizes(page, dir, "login");
  });

  for (const route of ROUTES) {
    test(route.name, async ({ page, consoleServer }, testInfo) => {
      const dir = path.join(OUT, testInfo.project.name);
      await page.setViewportSize(DESKTOP);
      if (typeof route.path === "string") {
        await signIn(page, consoleServer, route.path);
      } else {
        await signIn(page, consoleServer);
        await page.goto(await routePath(page, route));
      }
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      await captureSizes(page, dir, route.name);
    });
  }
});

/**
 * Screenshots of the new-app wizard, the Domains and certificates page and an application's
 * Domains tab, for review: both themes (the light and dark projects), a 1440px desktop and a
 * 390px phone. Tagged @screens, so left out of the default run; write them with
 *
 *     WASM_SCREENS=1 npx playwright test e2e/wizard-domains-screens.spec.ts
 *
 * into $WASM_WIZARD_SCREENS (default /tmp/console-wizard/<theme>/).
 *
 * The pages still pass the CSP and console gates of every test, so a screenshot is never of
 * a page that is quietly broken.
 */

import type { Page } from "@playwright/test";
import path from "node:path";

import { expect, settle, signIn, test, totpCode } from "./fixtures";
import type { ConsoleServer } from "./fixtures";
import { wizardSource } from "./wizard-sources";

const OUT = process.env.WASM_WIZARD_SCREENS ?? "/tmp/console-wizard";

const DESKTOP = { width: 1440, height: 900 };
const PHONE = { width: 390, height: 844 };

interface Screen {
  name: string;
  path: string;
  /** Brings the page to the state the screenshot is about. */
  act?: (page: Page, server: ConsoleServer) => Promise<void>;
  /** A console error the state causes on purpose. */
  expect?: RegExp;
}

/** Waits for every finite animation to end: a dialog opening, a state pulsing once. */
async function stillness(page: Page): Promise<void> {
  await page.waitForFunction(() =>
    document.getAnimations().every((animation) => {
      const iterations = animation.effect?.getComputedTiming().iterations;
      return animation.playState !== "running" || iterations === Infinity;
    }),
  );
}

async function inspected(page: Page): Promise<void> {
  await page.getByLabel("Repository or directory").fill(await wizardSource(page, "storefront"));
  await page.getByRole("button", { name: "Inspect source" }).click();
  await expect(page.getByRole("heading", { name: "Review", level: 2 })).toBeVisible();
}

const SCREENS: readonly Screen[] = [
  { name: "wizard-source", path: "/apps/new" },
  { name: "wizard-review", path: "/apps/new", act: inspected },
  {
    name: "wizard-deploy",
    path: "/apps/new",
    act: async (page) => {
      await inspected(page);
      await page.getByLabel("Domain", { exact: true }).fill("tienda-nueva.qrboda.com");
      for (const field of await page.getByRole("button", { name: /^Generate/ }).all()) await field.click();
      await page.getByLabel("DATABASE_URL").fill("postgres://storefront@localhost/storefront");
      await page.getByRole("button", { name: "Continue" }).click();
      await expect(page.getByRole("heading", { name: "Deploy", level: 2 })).toBeVisible();
    },
  },
  {
    name: "wizard-inspect-error",
    path: "/apps/new",
    // The API answers a source that cannot be fetched with a 500 (SourceError has no status of
    // its own in deps._STATUS_BY_ERROR), which Chromium logs as a failed resource.
    expect: /status of 500 .* \/api\/apps\/inspect$/,
    act: async (page) => {
      await page.getByLabel("Repository or directory").fill("/var/www/src/does-not-exist");
      await page.getByRole("button", { name: "Inspect source" }).click();
      await expect(page.getByText("Source path does not exist")).toBeVisible();
    },
  },
  { name: "domains-certificates", path: "/domains" },
  { name: "domains-sites", path: "/domains?tab=sites" },
  {
    name: "domains-issue",
    path: "/domains",
    act: async (page) => {
      await page.getByRole("button", { name: "Issue certificate" }).first().click();
      await expect(page.getByRole("dialog", { name: "Issue a certificate" })).toBeVisible();
    },
  },
  { name: "site-config", path: "/domains/sites/qrboda.com" },
  {
    name: "site-config-rejected",
    path: "/domains/sites/arennalabs.com",
    expect: /status of (403|400) .* \/api\/sites\/arennalabs\.com\/config$/,
    act: async (page, server) => {
      const editor = page.getByRole("textbox", { name: "Configuration of arennalabs.com" });
      await expect(editor).toHaveValue(/server_name/);
      const text = await editor.inputValue();
      await editor.fill(text.replace("proxy_http_version 1.1;", "proxy_http_version 1.1"));
      await page.getByRole("button", { name: "Test and save" }).click();
      const elevate = page.getByRole("dialog", { name: "Confirm it's you" });
      if (await elevate.isVisible({ timeout: 2_000 }).catch(() => false)) {
        await elevate.getByLabel("Authentication code").fill(totpCode(server.totpSecret ?? ""));
        await elevate.getByRole("button", { name: "Confirm" }).click();
      }
      await expect(page.getByText("Nothing was saved: the configuration test failed.")).toBeVisible();
    },
  },
  { name: "app-domains", path: "/apps/picconia.com/domains" },
  {
    name: "app-domains-failed",
    path: "/apps/qrboda.com/domains",
    act: async (page) => {
      if ((await page.getByText("soon.qrboda.com", { exact: true }).count()) > 0) return;
      await page.getByRole("button", { name: "Add domain" }).click();
      const dialog = page.getByRole("dialog", { name: "Add a domain to qrboda.com" });
      await dialog.getByLabel("Domain").fill("soon.qrboda.com");
      await dialog.getByRole("button", { name: "Check DNS" }).click();
      await dialog.getByRole("button", { name: "Add anyway" }).click();
      await expect(page.getByText("The certificate was not extended")).toBeVisible({ timeout: 20_000 });
      await page.locator('.toast button[aria-label="Dismiss notification"]').evaluateAll((buttons) => {
        for (const button of buttons) (button as HTMLButtonElement).click();
      });
    },
  },
  {
    name: "app-domains-add",
    path: "/apps/cittek.es/domains",
    act: async (page) => {
      await page.getByRole("button", { name: "Add domain" }).click();
      const dialog = page.getByRole("dialog", { name: "Add a domain to cittek.es" });
      await dialog.getByLabel("Domain").fill("old.cittek.es");
      await dialog.getByRole("button", { name: "Check DNS" }).click();
      await expect(dialog.getByText("old.cittek.es points somewhere else")).toBeVisible();
    },
  },
];

test.describe("wizard and domains screens @screens", () => {
  for (const screen of SCREENS) {
    test(screen.name, async ({ page, consoleServer, problems }, testInfo) => {
      if (screen.expect) problems.expect(screen.expect);
      const dir = path.join(OUT, testInfo.project.name);
      for (const [label, size] of [
        ["desktop", DESKTOP],
        ["mobile", PHONE],
      ] as const) {
        await page.setViewportSize(size);
        if (label === "desktop") await signIn(page, consoleServer, screen.path);
        else await page.goto(screen.path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await settle(page);
        await screen.act?.(page, consoleServer);
        await settle(page);
        await stillness(page);
        await page.screenshot({ path: path.join(dir, `${screen.name}-${label}.png`), fullPage: true });
      }
    });
  }
});

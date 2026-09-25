// Captures the console shell and the sign-in screens against the mock backend, for visual
// review: both themes, desktop and phone widths.
//
//   npm run dev:mock &        (VITE_MOCK=1: mock/backend.ts answers the API)
//   npm run screens:shell -- [--url http://127.0.0.1:5199] [--out /tmp/console-shell]
//
// Development aid only: it drives the dev server in mock mode (mock/backend.ts), never a
// build, and writes outside the repo. It also walks the flows that matter while it is there
// (two-step sign-in, "Confirm it's you", an expired session returning to where it was) and
// reports anything that did not end where it should.

import { mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";

import { chromium } from "@playwright/test";

const { values } = parseArgs({
  options: {
    url: { type: "string", default: "http://127.0.0.1:5199" },
    out: { type: "string", default: "/tmp/console-shell" },
  },
});

const TOKEN = "wasm_mock_token";
const CODE = "123456";
const THEMES = /** @type {const} */ (["light", "dark"]);
const WIDTHS = [1440, 390];

/** @type {string[]} */
const problems = [];

/**
 * @param {boolean} ok
 * @param {string} message
 */
function check(ok, message) {
  if (!ok) problems.push(message);
}

/**
 * Fires a request through the console's own API client (main.tsx exposes it as
 * window.__wasmDev in development), so the real error paths run: elevation, expiry. Passed to
 * page.evaluate as is: Playwright serialises it into the page, where it declares its own type.
 *
 * @param {{ method: string, path: string }} request
 */
function callDevApi({ method, path }) {
  const dev = /** @type {{ __wasmDev?: { api: (method: string, path: string) => Promise<unknown> } }} */ (
    /** @type {unknown} */ (window)
  ).__wasmDev;
  void dev?.api(method, path).catch(() => undefined);
}

/** @param {import("@playwright/test").Page} page */
async function settle(page) {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(350);
}

/** @param {import("@playwright/test").Page} page */
async function signIn(page) {
  await page.getByLabel("Access token").fill(TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByLabel("Two-factor code").waitFor();
  await page.getByLabel("Two-factor code").fill(CODE);
  await page.getByRole("button", { name: "Verify" }).click();
}

async function main() {
  const out = values.out;
  await rm(out, { recursive: true, force: true });
  const browser = await chromium.launch();

  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      const tag = `${theme}-${String(width)}`;
      const dir = path.join(out, tag);
      await mkdir(dir, { recursive: true });
      const mobile = width < 600;
      const context = await browser.newContext({
        viewport: { width, height: mobile ? 844 : 900 },
        colorScheme: theme,
        deviceScaleFactor: mobile ? 2 : 1,
        reducedMotion: "reduce",
      });
      const page = await context.newPage();
      page.on("console", (message) => {
        if (message.type() === "error" || message.type() === "warning") {
          // The walk provokes a 401 (expiry) and a 403 (elevation) on purpose; the browser logs
          // each failed request.
          if (/status of 40[13]/.test(message.text())) return;
          problems.push(`[${tag}] ${message.type()}: ${message.text()}`);
        }
      });
      page.on("pageerror", (error) => problems.push(`[${tag}] pageerror: ${error.message}`));
      const shot = (/** @type {string} */ name) => page.screenshot({ path: path.join(dir, `${name}.png`) });

      // Sign-in, both steps.
      await page.goto(`${values.url}/apps`, { waitUntil: "networkidle" });
      check(page.url().includes("/login?next=%2Fapps"), `[${tag}] an anonymous /apps did not land on /login?next=/apps: ${page.url()}`);
      await settle(page);
      await shot("01-login");
      await page.getByLabel("Access token").fill(TOKEN);
      await page.getByRole("button", { name: "Sign in" }).click();
      await page.getByLabel("Two-factor code").waitFor();
      await settle(page);
      await shot("02-login-second-factor");
      await page.getByLabel("Two-factor code").fill("000000");
      await page.getByRole("button", { name: "Verify" }).click();
      await page.getByText("Invalid two-factor code").waitFor();
      await settle(page);
      await shot("03-login-wrong-code");
      await page.getByLabel("Two-factor code").fill(CODE);
      await page.getByRole("button", { name: "Verify" }).click();
      await page.waitForURL(/\/apps$/);

      // The shell.
      await page.waitForSelector("h1");
      await page.waitForTimeout(600);
      await settle(page);
      await shot("04-apps");
      await page.goto(`${values.url}/`, { waitUntil: "networkidle" });
      await settle(page);
      await shot("05-overview");
      await page.goto(`${values.url}/apps/shop.example.com/logs`, { waitUntil: "networkidle" });
      await settle(page);
      await shot("06-app-logs-tab");
      await page.goto(`${values.url}/settings/security`, { waitUntil: "networkidle" });
      await settle(page);
      await shot("07-settings-security");

      // Palette.
      await page.goto(`${values.url}/apps`, { waitUntil: "networkidle" });
      await settle(page);
      if (mobile) await page.getByRole("button", { name: "Search" }).click();
      else await page.keyboard.press("Control+k");
      await page.getByRole("combobox").waitFor();
      await page.waitForTimeout(400);
      await shot("08-palette");
      await page.keyboard.type("sh");
      await page.waitForTimeout(200);
      await shot("09-palette-filtered");
      await page.keyboard.press("Escape");
      await page.waitForTimeout(300);

      // Mobile menu, or the session popover on desktop.
      if (mobile) {
        await page.getByRole("button", { name: "Open menu" }).click();
        await page.waitForTimeout(400);
        await shot("10-menu");
        await page.getByRole("link", { name: "Databases" }).click();
        await page.waitForURL(/\/databases$/);
        await page.waitForTimeout(400);
        const focused = await page.evaluate(() => document.activeElement?.tagName ?? "");
        check(focused === "H1", `[${tag}] after choosing a page in the menu, focus is on ${focused}, not the h1`);
      } else {
        await page.getByRole("button", { name: "Session and preferences" }).click();
        await page.waitForTimeout(300);
        await shot("10-session");
        await page.keyboard.press("Escape");
        await page.getByRole("link", { name: "Databases" }).click();
        await page.waitForURL(/\/databases$/);
        await page.waitForTimeout(200);
        const focused = await page.evaluate(() => document.activeElement?.tagName ?? "");
        check(focused === "H1", `[${tag}] after a sidebar navigation, focus is on ${focused}, not the h1`);
      }

      // Shortcuts dialog.
      await page.locator("body").click({ position: { x: 5, y: 300 } });
      await page.keyboard.press("?");
      await page.waitForTimeout(300);
      await shot("11-shortcuts");
      await page.keyboard.press("Escape");
      await page.waitForTimeout(200);

      // "Confirm it's you": a destructive call through the real client.
      await page.evaluate(callDevApi, { method: "DELETE", path: "/api/apps/admin.example.com" });
      await page.getByRole("dialog", { name: "Confirm it's you" }).waitFor();
      await page.waitForTimeout(300);
      await shot("12-elevate");
      await page.getByLabel("Authentication code").fill("111111");
      await page.getByRole("button", { name: "Confirm" }).click();
      await page.getByText("Invalid two-factor code").waitFor();
      await shot("13-elevate-wrong-code");
      await page.getByRole("button", { name: "Cancel" }).click();
      await page.waitForTimeout(300);

      // An expired session: the next request lands on sign-in, then back to the same page.
      await page.goto(`${values.url}/apps/shop.example.com/environment`, { waitUntil: "networkidle" });
      await page.evaluate(() => fetch("/api/__mock/expire", { method: "POST" }));
      await page.evaluate(callDevApi, { method: "GET", path: "/api/apps" });
      await page.waitForURL(/\/login\?/);
      check(page.url().includes("reason=expired"), `[${tag}] an expired session did not carry reason=expired: ${page.url()}`);
      await page.getByText("Your session expired").waitFor();
      await settle(page);
      await shot("14-login-expired");
      await signIn(page);
      await page.waitForURL(/\/apps\/shop\.example\.com\/environment$/);

      await context.close();
    }
  }
  await browser.close();
  if (problems.length > 0) console.log(`Problems:\n${problems.join("\n")}`);
  console.log(`Screenshots in ${out}`);
}

await main();

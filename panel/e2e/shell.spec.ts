/**
 * The console's shell against the real backend: sign-in with the second factor, every
 * destination of the sidebar, the command palette, and a session that ends while the
 * console is open. Runs once per theme (the light and dark projects), and every test fails
 * on a CSP violation or a console error through the `problems` fixture.
 */

import type { Page } from "@playwright/test";

import { expect, expectNoA11yViolations, settle, signIn, startConsoleServer, test, totpCode } from "./fixtures";

/** The sidebar, top to bottom, with the heading each destination opens on. */
const DESTINATIONS: readonly { link: string; path: string; title: string }[] = [
  { link: "Overview", path: "/", title: "Overview" },
  { link: "Applications", path: "/apps", title: "Applications" },
  { link: "Databases", path: "/databases", title: "Databases" },
  { link: "Services", path: "/services", title: "Services" },
  { link: "Cron", path: "/cron", title: "Cron" },
  { link: "Domains and certificates", path: "/domains", title: "Domains and certificates" },
  { link: "Backups", path: "/backups", title: "Backups" },
  { link: "Activity", path: "/activity", title: "Activity" },
  { link: "Server", path: "/server", title: "Server" },
  { link: "Settings", path: "/settings", title: "Settings" },
];

/** Chromium logs every failed fetch as a console error; these tests fail some on purpose. */
const REFUSED_SIGN_IN = /status of 401 .* \/api\/auth\/login$/;
const LOCKED_SIGN_IN = /status of 429 .* \/api\/auth\/login$/;
/** Once the session is gone, whichever requests are in flight are refused, not only one. */
const EXPIRED = /status of 401 .* \/api\/[\w/.-]+$/;

function heading(page: Page) {
  return page.getByRole("heading", { level: 1 });
}

/** Relative luminance of the page heading's text, 0 (black) to 1 (white). */
async function headingLuminance(page: Page): Promise<number> {
  return heading(page).evaluate((element) => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    if (!context) return 0.5;
    // Any colour syntax the stylesheet uses (oklch, light-dark) resolves through a canvas.
    // Text is always opaque, unlike a background that may live on any ancestor.
    context.fillStyle = getComputedStyle(element).color;
    context.fillRect(0, 0, 1, 1);
    const [r = 0, g = 0, b = 0] = context.getImageData(0, 0, 1, 1).data;
    const linear = (value: number) => {
      const c = value / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b);
  });
}

test("an anonymous visit lands on sign-in, which asks for the token and then the second factor", async ({
  page,
  consoleServer,
}) => {
  await page.goto("/apps");

  await expect(page).toHaveURL(/\/login\?next=%2Fapps$/);
  await expect(heading(page)).toHaveText("Sign in");
  await settle(page);
  await expectNoA11yViolations(page, "sign in");

  await page.getByLabel("Access token").fill(consoleServer.token);
  await page.getByRole("button", { name: "Sign in" }).click();

  const code = page.getByLabel("Two-factor code");
  await expect(code).toBeFocused();
  await expect(page.getByText("Access token accepted")).toBeVisible();
  await expectNoA11yViolations(page, "the second factor");

  if (consoleServer.totpSecret === null) throw new Error("the worker's server runs with two-factor on");
  await code.fill(totpCode(consoleServer.totpSecret));
  await page.getByRole("button", { name: "Verify" }).click();

  await expect(page).toHaveURL(/\/apps$/);
  await expect(heading(page)).toHaveText("Applications");
});

test("a wrong token is refused in the server's words and the field keeps focus", async ({ page, problems }) => {
  problems.expect(REFUSED_SIGN_IN);
  await page.goto("/login");

  await page.getByLabel("Access token").fill("wasm_not_the_token");
  await page.getByRole("button", { name: "Sign in" }).click();

  const token = page.getByLabel("Access token");
  await expect(token).toHaveAttribute("aria-invalid", "true");
  await expect(token).toBeFocused();
  await expectNoA11yViolations(page, "a refused token");
});

test("every destination renders its page with no accessibility or policy violation", async ({
  page,
  consoleServer,
}, testInfo) => {
  await signIn(page, consoleServer);

  // The console follows the operating system until a theme is pinned: the dark project
  // must actually be dark (light text), or axe would be checking the light palette twice.
  // Polled: the shell fades in, and a colour read mid-transition says nothing.
  const luminance = expect.poll(() => headingLuminance(page), { message: "the heading's text luminance" });
  if (testInfo.project.name === "dark") await luminance.toBeGreaterThan(0.6);
  else await luminance.toBeLessThan(0.2);

  const main = page.getByRole("navigation", { name: "Main" });
  for (const destination of DESTINATIONS) {
    await test.step(destination.title, async () => {
      const link =
        destination.link === "Settings"
          ? page.getByRole("link", { name: "Settings", exact: true })
          : main.getByRole("link", { name: new RegExp(`^${destination.link}`) });
      await link.click();

      await expect(page).toHaveURL(new RegExp(`${destination.path === "/" ? "/" : destination.path}$`));
      await expect(heading(page)).toHaveText(destination.title);
      await expect(link).toHaveAttribute("aria-current", "page");
      await settle(page);
      await expectNoA11yViolations(page, destination.title);
    });
  }
});

test("the command palette opens with Ctrl+K, filters, navigates on Enter and closes on Escape", async ({
  page,
  consoleServer,
}) => {
  await signIn(page, consoleServer);
  await settle(page);
  const palette = page.getByRole("dialog");
  const search = page.getByRole("combobox", { name: "Search pages, applications and actions" });

  await page.keyboard.press("Control+k");
  await expect(palette).toBeVisible();
  await expect(search).toBeFocused();
  await expectNoA11yViolations(page, "the command palette");

  await search.fill("cron");
  await expect(page.getByRole("option").first()).toContainText("Cron");
  await page.keyboard.press("Enter");
  await expect(palette).toBeHidden();
  await expect(page).toHaveURL(/\/cron$/);
  await expect(heading(page)).toHaveText("Cron");

  // Applications come from the API: the seeded machine's apps are searchable.
  await page.keyboard.press("Control+k");
  await search.fill("picconia");
  await expect(page.getByRole("option", { name: /picconia\.com/ })).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/apps\/picconia\.com$/);
  await expect(heading(page)).toHaveText("picconia.com");

  // Opened from the search button, Escape hands focus back to it.
  const trigger = page.getByRole("button", { name: /^Search/ }).first();
  await trigger.click();
  await expect(palette).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("a session that ends while the console is open returns to the same page after signing in", async ({
  page,
  context,
  consoleServer,
  problems,
}) => {
  problems.expect(EXPIRED);
  await signIn(page, consoleServer);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /^Databases/ }).click();
  await expect(heading(page)).toHaveText("Databases");

  // The session is gone as far as the browser can tell; the next request is refused.
  await context.clearCookies();
  await page.keyboard.press("Control+k");

  await expect(page).toHaveURL(/\/login\?next=%2Fdatabases&reason=expired$/);
  await expect(page.getByText("Your session expired. Sign in again to continue where you left off.")).toBeVisible();
  await expectNoA11yViolations(page, "the expired-session notice");

  await signIn(page, consoleServer);
  await expect(page).toHaveURL(/\/databases$/);
  await expect(heading(page)).toHaveText("Databases");
});

test("a locked-out address is told how long to wait", async ({ page, problems }) => {
  problems.expect(LOCKED_SIGN_IN);
  // Its own server: a lockout holds the whole address for fifteen minutes, and the
  // worker's server is shared with every other test in the worker.
  const server = await startConsoleServer();
  try {
    await page.goto(`${server.url}/login`);
    const token = page.getByLabel("Access token");
    const submit = page.getByRole("button", { name: "Sign in" });
    for (let attempt = 0; attempt < 6; attempt += 1) {
      await token.fill(`wasm_wrong_${String(attempt)}`);
      await submit.click();
      await expect(submit).toBeEnabled({ timeout: 5_000 }).catch(() => undefined);
      if (await page.getByText("Too many failed attempts", { exact: true }).isVisible()) break;
    }

    await expect(page.getByText("Too many failed attempts", { exact: true })).toBeVisible();
    // In the form; the assertive live region repeats the server's words for screen readers.
    await expect(page.locator("form").getByText(/Try again in \d+:\d\d/)).toBeVisible();
    await expect(submit).toBeDisabled();
    await expectNoA11yViolations(page, "the lockout notice");
  } finally {
    await server.stop();
  }
});

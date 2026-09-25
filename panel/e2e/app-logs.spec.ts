/**
 * An application's journal against the real backend: the logs WebSocket streams the seeded
 * unit's journal (answered by the console server's model of journalctl), `/` searches it and
 * counts the matches, and a static site says it has no process to log. Both themes, with the
 * CSP and console gates of the `problems` fixture.
 */

import { expect, expectNoA11yViolations, settle, signIn, test } from "./fixtures";

const DOMAIN = "tienda.cittek.es";

test("the journal streams in, `/` searches it and counts the matches", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, `/apps/${DOMAIN}/logs`);
  const journal = page.getByRole("region", { name: `Journal of ${DOMAIN}` });
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  // Following: the newest lines are the ones in view.
  await expect(journal.getByText(/tienda-cittek-es\[\d+\]: /).last()).toBeVisible();
  const count = page.getByText(/^\d[\d,]* lines$/);
  await expect(count).toBeVisible();
  const before = Number.parseInt(((await count.textContent()) ?? "0").replace(/,/g, ""), 10);
  // The unit keeps writing while it is followed.
  await expect
    .poll(async () => Number.parseInt(((await count.textContent()) ?? "0").replace(/,/g, ""), 10), { timeout: 10_000 })
    .toBeGreaterThan(before);

  await journal.click();
  await page.keyboard.press("/");
  const search = page.getByRole("searchbox", { name: "Search output" });
  await expect(search).toBeFocused();
  await page.keyboard.type("ECONNREFUSED");
  await expect(page.getByText(/^1 of \d+$/)).toBeVisible();
  await expect(journal.locator("mark").first()).toHaveText("ECONNREFUSED");

  await settle(page);
  await expectNoA11yViolations(page, "an app's journal");
});

test("a static site says it has no process to log", async ({ page, consoleServer }) => {
  await signIn(page, consoleServer, "/apps/taller.arennalabs.com/logs");
  await expect(page.getByRole("heading", { level: 2, name: "A static site has no process to log" })).toBeVisible();
  await settle(page);
  await expectNoA11yViolations(page, "a static site's logs tab");
});

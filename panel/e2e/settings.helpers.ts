/**
 * What the Settings specs share: waiting for motion to stop before axe measures colour, the
 * "Confirm it's you" step, and the browser-side check of the toast's accessibility.
 */

import type { Page } from "@playwright/test";

import { confirmItsYou, expect, stillness } from "./fixtures";

// stillness is fixtures.ts's own (it also waits inside settle()); re-exported here so specs
// that already import it from this module keep working with one implementation behind it.
export { confirmItsYou, stillness };

/** The visible toast (not its announcement) that says `text`. */
export function toastSaying(page: Page, text: string | RegExp) {
  return page.locator(".toast").filter({ hasText: text });
}

/**
 * Holds a toast on screen: a pointer over the stack pauses its timer, the way an operator
 * reading it would. A success toast otherwise leaves after five seconds, which axe can outlast.
 */
export async function holdToast(page: Page, text: string | RegExp): Promise<void> {
  const toast = toastSaying(page, text);
  await expect(toast).toBeVisible();
  await toast.hover();
}

/**
 * The toast's accessibility in a real browser: the visible toast is announced by exactly one
 * live region with no hidden copy, nothing focusable is hidden from assistive technology,
 * and it is dismissible from the keyboard.
 */
export async function expectAccessibleToast(page: Page, text: string, politeness: "polite" | "assertive"): Promise<void> {
  const toast = toastSaying(page, text);
  await expect(toast).toBeVisible();
  const report = await page.evaluate((said) => {
    const liveSelector = '[aria-live]:not([aria-live="off"])';
    const live = [...document.querySelectorAll(liveSelector)].filter(
      (region) => region.textContent.includes(said) && !region.parentElement?.closest(liveSelector),
    );
    const focusable = ':is(a[href], button, input, select, textarea, [tabindex]):not([tabindex="-1"]):not([disabled])';
    const hiddenFocusables = [...document.querySelectorAll('[aria-hidden="true"]')].flatMap((hidden) =>
      [...(hidden.matches(focusable) ? [hidden] : []), ...hidden.querySelectorAll(focusable)].map((el) => el.outerHTML.slice(0, 100)),
    );
    const visible = [...document.querySelectorAll(".toast")].find((el) => el.textContent.includes(said));
    // Every element whose own text says it: one copy, the visible one.
    const copies = [...document.querySelectorAll("body *")].filter((el) =>
      [...el.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && (node.textContent ?? "").includes(said)),
    );
    return {
      regions: live.map((region) => region.getAttribute("aria-live")),
      hiddenFocusables,
      visibleIsAnnounced: visible !== undefined && live.some((region) => region.contains(visible)),
      copies: copies.length,
    };
  }, text);
  expect(report.regions, "the toast is announced by exactly one live region").toEqual([politeness]);
  expect(report.visibleIsAnnounced, "the live region holds the visible toast").toBe(true);
  expect(report.copies, "no second, hidden copy of the words").toBe(1);
  expect(report.hiddenFocusables, "no focusable element is hidden from assistive technology").toEqual([]);

  // Reachable and operable from the keyboard.
  const dismiss = toast.getByRole("button", { name: "Dismiss notification" });
  await dismiss.focus();
  await expect(dismiss).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(toast).toHaveCount(0);
}

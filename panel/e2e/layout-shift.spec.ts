/**
 * Cumulative Layout Shift of every page, in both themes, measured as the page loads cold: a
 * PerformanceObserver on `layout-shift` is installed before any of the page's own scripts run,
 * and the score is read once the page has settled. Shifts that follow user input are excluded
 * (the browser flags them `hadRecentInput`), and none is made: the test only loads the page.
 *
 * The score is the web-vitals definition - the largest session window of shifts less than a
 * second apart, capped at five seconds - and a page fails above 0.05, half of what the metric
 * itself calls "good": the console is an instrument, and a row that jumps as it is about to
 * be clicked is a misclick. On failure the message lists the elements that moved, largest
 * shift first, so the cause is fixed at its source rather than guessed at.
 *
 * With WASM_CLS_REPORT set to a directory, each page's score is written there as JSON too,
 * for a before-and-after table.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

import { expect, settle, signIn, test } from "./fixtures";
import { ROUTES, routePath } from "./routes";

const DESKTOP = { width: 1440, height: 900 };
const LIMIT = 0.05;
/** Time after the page settles for anything late (a chart's second query, a font) to land. */
const TAIL_MS = 1_500;

interface ShiftRecord {
  value: number;
  time: number;
  sources: string[];
}

declare global {
  interface Window {
    __wasmShifts?: ShiftRecord[];
  }
}

/** Installed before the page's own scripts: records every shift not caused by input. */
function observeLayoutShifts(): void {
  interface LayoutShift extends PerformanceEntry {
    value: number;
    hadRecentInput: boolean;
    sources: { node: Node | null; previousRect: DOMRectReadOnly; currentRect: DOMRectReadOnly }[];
  }
  const describe = (node: Node | null): string => {
    if (node === null) return "?";
    if (!(node instanceof Element)) return node.nodeName;
    const id = node.id ? `#${node.id}` : "";
    const cls = typeof node.className === "string" ? node.className.split(/\s+/).filter(Boolean).slice(0, 4).join(".") : "";
    const text = node.textContent.replace(/\s+/g, " ").trim().slice(0, 40);
    return `<${node.tagName.toLowerCase()}${id}${cls ? `.${cls}` : ""}> "${text}"`;
  };
  const shifts: ShiftRecord[] = [];
  window.__wasmShifts = shifts;
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries() as LayoutShift[]) {
      if (entry.hadRecentInput) continue;
      shifts.push({
        value: entry.value,
        time: entry.startTime,
        sources: entry.sources.map(
          (source) =>
            `${describe(source.node)} ${String(Math.round(source.previousRect.y))}->${String(Math.round(source.currentRect.y))}` +
            ` h${String(Math.round(source.previousRect.height))}->${String(Math.round(source.currentRect.height))}`,
        ),
      });
    }
  }).observe({ type: "layout-shift", buffered: true });
}

/** The web-vitals CLS: the largest window of shifts under 1 s apart and within 5 s overall. */
function cumulativeLayoutShift(shifts: readonly ShiftRecord[]): number {
  let worst = 0;
  let current = 0;
  let first = 0;
  let previous = 0;
  for (const shift of shifts) {
    if (current > 0 && (shift.time - previous >= 1_000 || shift.time - first >= 5_000)) {
      current = 0;
    }
    if (current === 0) first = shift.time;
    current += shift.value;
    previous = shift.time;
    worst = Math.max(worst, current);
  }
  return worst;
}

for (const route of ROUTES) {
  test(`${route.name} does not shift as it loads`, async ({ page, consoleServer }, testInfo) => {
    await page.setViewportSize(DESKTOP);
    await signIn(page, consoleServer);
    const target = await routePath(page, route);

    // A cold load of the page itself, not the client-side navigation that follows sign-in.
    await page.addInitScript(observeLayoutShifts);
    await page.goto(target);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await settle(page);
    await page.waitForTimeout(TAIL_MS);

    const shifts = await page.evaluate(() => window.__wasmShifts ?? []);
    const score = cumulativeLayoutShift(shifts);

    const report = process.env.WASM_CLS_REPORT;
    if (report) {
      mkdirSync(report, { recursive: true });
      writeFileSync(
        path.join(report, `${route.name}-${testInfo.project.name}.json`),
        JSON.stringify({ route: route.name, theme: testInfo.project.name, cls: score, shifts }, null, 2),
      );
    }

    const culprits = [...shifts]
      .sort((a, b) => b.value - a.value)
      .slice(0, 6)
      .map((shift) => `  ${shift.value.toFixed(4)} at ${String(Math.round(shift.time))}ms\n    ${shift.sources.join("\n    ")}`)
      .join("\n");
    expect(score, `layout shift of ${target} is ${score.toFixed(4)}:\n${culprits}`).toBeLessThanOrEqual(LIMIT);
  });
}

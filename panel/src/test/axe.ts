import axe from "axe-core";
import { expect } from "vitest";

/**
 * Runs axe-core against a rendered subtree and fails with a readable report.
 *
 * vitest-axe is unmaintained, so this calls axe directly. Colour contrast is disabled here
 * because jsdom does not compute styles; contrast is verified on the tokens themselves
 * (styles/tokens.test.ts) and in the browser by the E2E suite.
 */
export async function expectNoAxeViolations(container: Element): Promise<void> {
  const results = await axe.run(container, {
    rules: {
      "color-contrast": { enabled: false },
      // A component rendered in isolation is not a page; landmark rules apply in the E2E.
      region: { enabled: false },
    },
  });
  const report = results.violations.map(
    (v) => `${v.id}: ${v.help}\n${v.nodes.map((n) => `  ${n.html}`).join("\n")}`,
  );
  expect(report, report.join("\n\n")).toEqual([]);
}

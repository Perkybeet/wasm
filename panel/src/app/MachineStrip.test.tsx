import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { renderConsole } from "../test/console";
import { MACHINE, fakeBackend, json, signedInRoutes } from "../test/fakes";
import { unitTallySummary } from "./MachineStrip";

async function stripAt(units: typeof MACHINE.units) {
  fakeBackend({ ...signedInRoutes(), "GET /api/system/machine": () => json(200, { ...MACHINE, units }) });
  const harness = renderConsole("/apps");
  await screen.findByRole("heading", { level: 1 });
  const strip = screen.getByRole("group", { name: "This machine" });
  await within(strip).findByText("web-01");
  return { ...harness, strip };
}

describe("unitTallySummary", () => {
  it("says what each count means, in one sentence", () => {
    expect(unitTallySummary({ running: 15, failed: 0, stopped: 1 })).toBe("WASM units: 15 running, 0 failed, 1 stopped");
  });
});

describe("the unit tally", () => {
  it("names itself the same sentence it shows as a tooltip, not two different ones", async () => {
    const { strip } = await stripAt({ running: 9, failed: 1, stopped: 2 });
    const link = within(strip).getByRole("link", { name: "WASM units: 9 running, 1 failed, 2 stopped" });
    expect(link).toHaveAttribute("href", "/services");
  });

  it("keeps the visible text compact: the symbols and counts, not the sentence", async () => {
    const { strip } = await stripAt({ running: 9, failed: 1, stopped: 2 });
    const link = within(strip).getByRole("link", { name: "WASM units: 9 running, 1 failed, 2 stopped" });
    expect(link.textContent).toBe("Units912");
  });

  it("shows the same sentence as a tooltip on keyboard focus", async () => {
    const { strip } = await stripAt({ running: 9, failed: 1, stopped: 2 });
    const link = within(strip).getByRole("link", { name: "WASM units: 9 running, 1 failed, 2 stopped" });
    link.focus();
    expect(await screen.findByText("WASM units: 9 running, 1 failed, 2 stopped", {}, { timeout: 2000 })).toBeInTheDocument();
  });

  it("has no accessibility violations, with a failure to show", async () => {
    const { strip } = await stripAt({ running: 9, failed: 1, stopped: 2 });
    await expectNoAxeViolations(strip);
  });
});

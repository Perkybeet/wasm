import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ObservationList } from "../../api/queries/monitor";
import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { SYSTEM_HEALTH, fakeBackend, json, signedInRoutes } from "../../test/fakes";
import { checkName } from "./data";

const XMRIG_OBSERVATION: ObservationList["observations"][number] = {
  id: 91,
  observed_at: "2026-09-25T17:00:00Z",
  pid: 9931,
  process_name: "xmrig",
  user: "www-data",
  cpu_percent: 97.2,
  memory_percent: 1.1,
  command: "xmrig -o pool.example.com:4444",
  signal: "name-pattern",
  severity: "warning",
  detail: "Process name matches a known cryptominer pattern",
  acknowledged: false,
};

function withObservations(observations: ObservationList["observations"]) {
  return {
    ...signedInRoutes(),
    "GET /api/monitor/observations": () => json(200, { observations, count: observations.length, stats: {} }),
  };
}

/**
 * `signedInRoutes()` already answers every other endpoint this page reads (health, system
 * info, network, processes, the monitor's own status) with the seeded machine's fixtures -
 * see `test/fakes.ts`. Observations are supplied per test, since most other pages' tests
 * assume the shared default (none open).
 */
async function serverPage(observations: ObservationList["observations"] = []) {
  const backend = fakeBackend(withObservations(observations));
  const harness = renderConsole("/server");
  await screen.findByRole("heading", { level: 1, name: "Server" });
  return { ...harness, backend };
}

describe("the server page", () => {
  it("shows the health verdict and its checks", async () => {
    await serverPage();
    const health = await screen.findByRole("heading", { name: "Health" });
    const section = health.closest("section");
    if (!section) throw new Error("no section");
    expect(within(section).getByText("Healthy")).toBeInTheDocument();
    for (const check of SYSTEM_HEALTH.checks) {
      expect(within(section).getByText(checkName(check.name))).toBeInTheDocument();
    }
  });

  it("shows system info, network and top processes", async () => {
    await serverPage();
    const systemHeading = await screen.findByRole("heading", { name: "System" });
    const systemSection = systemHeading.closest("section");
    if (!systemSection) throw new Error("no section");
    expect(within(systemSection).getByText("web-01")).toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "Network interfaces" })).toBeInTheDocument();
    // "Top processes" is both the Section's own heading (an implicit region, since a <section>
    // with an accessible name is one) and the DataTable's caption inside it - two regions of
    // the same name, so this looks the table up by its heading instead of by role name.
    const processesHeading = await screen.findByRole("heading", { name: "Top processes" });
    expect(processesHeading.closest("section")).not.toBeNull();
    expect(screen.getByText("node")).toBeInTheDocument();
  });

  it("re-sorts top processes from the Select without a page reload", async () => {
    const { backend } = await serverPage();
    await screen.findByRole("heading", { name: "Top processes" });
    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByRole("combobox", { name: "Sort processes by" }));
    await user.click(await screen.findByRole("option", { name: "By memory" }));
    await waitFor(() => {
      expect(backend.callsTo("GET /api/system/processes").some((call) => call.search.get("sort_by") === "memory")).toBe(true);
    });
  });

  it("shows the resource monitor with its open finding", async () => {
    await serverPage([XMRIG_OBSERVATION]);
    const monitor = await screen.findByRole("region", { name: "Resource monitor" });
    expect(within(monitor).getByText("Running")).toBeInTheDocument();
    expect(within(monitor).getByText("xmrig")).toBeInTheDocument();
  });

  it("says nothing is open when the monitor has no findings", async () => {
    await serverPage([]);
    const monitor = await screen.findByRole("region", { name: "Resource monitor" });
    expect(await within(monitor).findByText(/Nothing open right now/)).toBeInTheDocument();
  });

  it("acknowledging a finding calls the API and removes it from the list", async () => {
    const backend = fakeBackend({
      ...withObservations([XMRIG_OBSERVATION]),
      "POST /api/monitor/observations/91/acknowledge": () => json(200, { success: true, message: "Observation 91 acknowledged" }),
    });
    renderConsole("/server");
    const monitor = await screen.findByRole("region", { name: "Resource monitor" });
    const user = (await import("@testing-library/user-event")).default.setup();
    const acknowledge = await within(monitor).findByRole("button", { name: /Acknowledge finding about xmrig/ });
    await user.click(acknowledge);
    await waitFor(() => {
      expect(backend.callsTo("POST /api/monitor/observations/91/acknowledge")).toHaveLength(1);
    });
  });

  it("has no accessibility violations", async () => {
    await serverPage([XMRIG_OBSERVATION]);
    await screen.findByRole("heading", { name: "Top processes" });
    await within(await screen.findByRole("region", { name: "Resource monitor" })).findByText("xmrig");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

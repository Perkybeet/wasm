import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const JOBS = [
  {
    name: "nightly-backup",
    command: "wasm backup create shop.example.com",
    user: "wasm",
    working_directory: "/var/www/shop",
    app_domain: "shop.example.com",
    schedule: "daily",
    on_calendar: "*-*-* 02:00:00",
    enabled: true,
    next_run: "Fri 2026-09-26 02:00:00 UTC",
    last_run: "Thu 2026-09-25 02:00:00 UTC",
    last_exit_code: 0,
    last_result: "success",
  },
  {
    name: "hourly-sync",
    command: "rsync -a /a /b",
    user: "wasm",
    working_directory: "",
    app_domain: "",
    schedule: "hourly",
    on_calendar: "*-*-* *:00:00",
    enabled: false,
    next_run: "pending",
    last_run: "never",
    last_exit_code: null,
    last_result: "never ran",
  },
];

async function cronAt(path = "/cron", extra: Record<string, RouteHandler> = {}) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/cron": () => json(200, { jobs: JOBS, total: JOBS.length }),
    ...extra,
  });
  const harness = renderConsole(path);
  await screen.findByRole("heading", { level: 1, name: "Cron" });
  const table = await screen.findByRole("region", { name: /Cron jobs/ });
  return { ...harness, backend, table };
}

describe("the cron jobs list", () => {
  it("lists every seeded job with its schedule and last result", async () => {
    const { table } = await cronAt();
    const enabledRow = (await within(table).findByText("nightly-backup")).closest("tr");
    if (!enabledRow) throw new Error("no row");
    expect(within(enabledRow).getByText("Enabled")).toBeInTheDocument();
    expect(within(enabledRow).getByText("Succeeded")).toBeInTheDocument();

    const disabledRow = within(table).getByText("hourly-sync").closest("tr");
    if (!disabledRow) throw new Error("no row");
    // The State column's pill, not the Next Run column: a disabled job's next run is "-" with
    // an sr-only reason of "Disabled" too.
    const stateCell = within(disabledRow).getAllByRole("cell")[0];
    if (!stateCell) throw new Error("no state cell");
    expect(within(stateCell).getByText("Disabled")).toBeInTheDocument();
    expect(within(disabledRow).getByText("Never run")).toBeInTheDocument();
  });

  it("filters by the search box, written into the URL", async () => {
    const { table, location } = await cronAt();
    await within(table).findByText("nightly-backup");
    const user = (await import("@testing-library/user-event")).default.setup();
    await user.type(screen.getByRole("searchbox", { name: "Search cron jobs" }), "hourly");
    await waitFor(() => {
      expect(location().search).toEqual({ q: "hourly" });
    });
    expect(within(table).queryByText("nightly-backup")).not.toBeInTheDocument();
  });

  it("creates a job through POST /api/cron and reports the next run", async () => {
    const { user, backend, table } = await cronAt("/cron", {
      "POST /api/cron": () =>
        json(201, {
          success: true,
          message: "Cron job e2e-report created",
          job: { ...JOBS[0], name: "e2e-report", next_run: "Fri 2026-09-26 02:00:00 UTC" },
        }),
    });
    await within(table).findByText("nightly-backup");
    await user.click(screen.getByRole("button", { name: "New job" }));
    const dialog = await screen.findByRole("dialog", { name: "New cron job" });
    await user.type(within(dialog).getByLabelText("Name", { exact: true }), "e2e-report");
    await user.type(within(dialog).getByLabelText("Command", { exact: true }), "/usr/bin/wasm backup create example.com");
    await user.click(within(dialog).getByRole("button", { name: "Create job" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/cron")).toHaveLength(1);
    });
    expect(backend.callsTo("POST /api/cron")[0]?.body).toMatchObject({ name: "e2e-report", schedule: "*-*-* 02:00:00" });
    expect(await screen.findByText("Created e2e-report")).toBeInTheDocument();
  });

  it("invites the first job on a machine with none scheduled", async () => {
    fakeBackend({ ...signedInRoutes(), "GET /api/cron": () => json(200, { jobs: [], total: 0 }) });
    renderConsole("/cron");
    expect(await screen.findByRole("heading", { level: 2, name: "Schedule your first job" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { table } = await cronAt();
    await within(table).findByText("nightly-backup");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

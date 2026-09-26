import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ServiceList } from "../../api/queries/services";
import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const SERVICES: ServiceList["services"] = [
  {
    name: "wasm-shop-example-com",
    description: "/usr/bin/node server.js",
    active: true,
    enabled: true,
    status: "running",
    pid: 4213,
    uptime: "Wed 2026-09-24 10:00:00 UTC",
    memory: "104857600",
    managed: true,
    active_state: "active",
    sub_state: "running",
    result: "success",
  },
  {
    name: "wasm-admin-example-com",
    description: "/usr/bin/node server.js",
    active: false,
    enabled: false,
    status: "stopped",
    pid: null,
    uptime: null,
    memory: null,
    managed: true,
    active_state: "inactive",
    sub_state: "dead",
    result: "success",
  },
];

/** The machine's units, once "Show all units" asks for `wasm_only=false`: WASM's own, plus one it did not create. */
const ALL_SERVICES: ServiceList["services"] = [
  ...SERVICES,
  {
    name: "postgresql",
    description: null,
    active: true,
    enabled: true,
    status: "running",
    pid: 908,
    uptime: "Wed 2026-09-24 09:00:00 UTC",
    memory: "31457280",
    managed: false,
    active_state: "active",
    sub_state: "running",
    result: "success",
  },
];

/** Answers `GET /api/services` scoped by `wasm_only`, the way the real endpoint does. */
function scopedServicesRoute(): RouteHandler {
  return (call) =>
    call.search.get("wasm_only") === "false"
      ? json(200, { services: ALL_SERVICES, total: ALL_SERVICES.length })
      : json(200, { services: SERVICES, total: SERVICES.length });
}

async function servicesAt(path = "/services", extra: Record<string, RouteHandler> = {}) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/services": () => json(200, { services: SERVICES, total: SERVICES.length }),
    ...extra,
  });
  const harness = renderConsole(path);
  await screen.findByRole("heading", { level: 1, name: "Services" });
  const table = await screen.findByRole("region", { name: /Services/ });
  return { ...harness, backend, table };
}

describe("the services list", () => {
  it("lists every seeded service with its state and boot setting", async () => {
    const { table } = await servicesAt();
    const running = await within(table).findByText("wasm-shop-example-com");
    const row = running.closest("tr");
    if (!row) throw new Error("no row");
    expect(within(row).getByText("Running")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();

    const stopped = within(table).getByText("wasm-admin-example-com").closest("tr");
    if (!stopped) throw new Error("no row");
    expect(within(stopped).getByText("Stopped")).toBeInTheDocument();
    expect(within(stopped).getByText("Disabled")).toBeInTheDocument();
  });

  it("filters by the search box, written into the URL", async () => {
    const { table, location } = await servicesAt();
    await within(table).findByText("wasm-shop-example-com");
    const search = screen.getByRole("searchbox", { name: "Search services" });
    search.focus();
    const user = (await import("@testing-library/user-event")).default.setup();
    await user.type(search, "admin");
    await waitFor(() => {
      expect(location().search).toEqual({ q: "admin" });
    });
    expect(within(table).queryByText("wasm-shop-example-com")).not.toBeInTheDocument();
  });

  it("creates a service in simple mode through POST /api/services", async () => {
    const { user, backend, table } = await servicesAt("/services", {
      "POST /api/services": () => json(200, { success: true, message: "Service created: e2e-worker", service: "e2e-worker" }),
    });
    await within(table).findByText("wasm-shop-example-com");
    await user.click(screen.getByRole("button", { name: "New service" }));
    const dialog = await screen.findByRole("dialog", { name: "New service" });
    await user.type(within(dialog).getByLabelText("Name", { exact: true }), "e2e-worker");
    await user.type(within(dialog).getByLabelText("Command", { exact: true }), "/usr/bin/node worker.js");
    await user.click(within(dialog).getByRole("button", { name: "Create service" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/services")).toHaveLength(1);
    });
    expect(backend.callsTo("POST /api/services")[0]?.body).toMatchObject({ name: "e2e-worker", command: "/usr/bin/node worker.js" });
    expect(await screen.findByText("Created e2e-worker")).toBeInTheDocument();
  });

  it("invites the first service on an empty machine", async () => {
    fakeBackend({ ...signedInRoutes(), "GET /api/services": () => json(200, { services: [], total: 0 }) });
    renderConsole("/services");
    expect(await screen.findByRole("heading", { level: 2, name: "Create your first service" })).toBeInTheDocument();
  });

  it("scopes the request to WASM's own units by default", async () => {
    const { backend, table } = await servicesAt("/services", { "GET /api/services": scopedServicesRoute() });
    await within(table).findByText("wasm-shop-example-com");
    await waitFor(() => {
      expect(backend.callsTo("GET /api/services").some((call) => call.search.get("wasm_only") === "true")).toBe(true);
    });
    expect(within(table).queryByText("postgresql")).not.toBeInTheDocument();
    // Every row is WASM's: a column saying so on each would say nothing.
    expect(within(table).queryByRole("columnheader", { name: /Managed/ })).not.toBeInTheDocument();
  });

  it("shows every unit, including a foreign one read-only, behind the show-all-units toggle", async () => {
    const { user, backend, table, location } = await servicesAt("/services", { "GET /api/services": scopedServicesRoute() });
    await within(table).findByText("wasm-shop-example-com");

    await user.click(screen.getByRole("switch", { name: "Show all units" }));

    await waitFor(() => {
      expect(backend.callsTo("GET /api/services").some((call) => call.search.get("wasm_only") === "false")).toBe(true);
    });
    expect(location().search).toEqual({ all: true });

    const foreignRow = (await within(table).findByText("postgresql")).closest("tr");
    expect(within(table).getByRole("columnheader", { name: /Managed/ })).toBeInTheDocument();
    if (!foreignRow) throw new Error("no row");
    // Once in its Managed column, once beside its name for phones (CSS shows one or the other).
    expect(within(foreignRow).getAllByText("Foreign")).toHaveLength(2);
    expect(within(foreignRow).queryByRole("button", { name: /Actions for/ })).not.toBeInTheDocument();

    const managedRow = within(table).getByText("wasm-shop-example-com").closest("tr");
    if (!managedRow) throw new Error("no row");
    expect(within(managedRow).getByRole("button", { name: /Actions for/ })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { table } = await servicesAt();
    await within(table).findByText("wasm-shop-example-com");
    await expectNoAxeViolations(screen.getByRole("main"));
  });

  it("has no accessibility violations with every unit shown", async () => {
    const { user, table } = await servicesAt("/services", { "GET /api/services": scopedServicesRoute() });
    await within(table).findByText("wasm-shop-example-com");
    await user.click(screen.getByRole("switch", { name: "Show all units" }));
    await within(table).findByText("postgresql");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

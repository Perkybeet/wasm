import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const SERVICES = [
  { name: "wasm-shop-example-com", description: "/usr/bin/node server.js", active: true, enabled: true, status: "running", pid: 4213, uptime: "Wed 2026-09-24 10:00:00 UTC", memory: "104857600" },
  { name: "wasm-admin-example-com", description: "/usr/bin/node server.js", active: false, enabled: false, status: "stopped", pid: null, uptime: null, memory: null },
];

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

  it("has no accessibility violations", async () => {
    const { table } = await servicesAt();
    await within(table).findByText("wasm-shop-example-com");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

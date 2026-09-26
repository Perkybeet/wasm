import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Service, ServiceList } from "../../api/queries/services";
import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { FakeWebSocket, fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const NAME = "wasm-worker";

const SERVICE: Service = {
  name: NAME,
  description: "/usr/bin/node worker.js",
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
};

const UNIT_FILE = "[Unit]\nDescription=WASM Service: wasm-worker\n\n[Service]\nExecStart=/usr/bin/node worker.js\n";

async function serviceDetailAt(name = NAME, extra: Record<string, RouteHandler> = {}) {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  const backend = fakeBackend({
    ...signedInRoutes(),
    [`GET /api/services/${name}`]: () => json(200, SERVICE),
    [`GET /api/services/${name}/config`]: () => json(200, { service: name, config: UNIT_FILE, path: `/etc/systemd/system/${name}.service` }),
    "POST /api/auth/ws-ticket": () => json(200, { ticket: "t1" }),
    ...extra,
  });
  const harness = renderConsole(`/services/${name}`);
  await screen.findByRole("heading", { level: 1, name });
  return { ...harness, backend };
}

describe("the unit editor", () => {
  it("blocks the save and shows systemd-analyze's own output when it rejects the unit", async () => {
    const { user, backend } = await serviceDetailAt(NAME, {
      "POST /api/services/verify": () =>
        json(200, { success: false, output: "wasm-verify.service: Service has no ExecStart= setting. Refusing.\n" }),
    });
    const textarea = await screen.findByLabelText(`Unit file for ${NAME}`, { exact: true });
    await user.type(textarea, "# edited");
    await user.click(screen.getByRole("button", { name: "Save unit file" }));

    expect(await screen.findByText(/systemd-analyze rejected the unit/)).toBeInTheDocument();
    expect(screen.getByText(/Service has no ExecStart= setting\. Refusing\./)).toBeInTheDocument();
    await waitFor(() => {
      expect(backend.callsTo("POST /api/services/verify")).toHaveLength(1);
    });
    expect(backend.callsTo(`PUT /api/services/${NAME}/config`)).toHaveLength(0);
  });

  it("saves once systemd-analyze passes, behind confirming it's you, and says it found no problems", async () => {
    // The fake mirrors what `require_elevated` does on the real backend: the first,
    // unelevated attempt is refused, and the client's own retry after "Confirm it's you"
    // succeeds - the same shape the elevation E2E test exercises against the real API.
    let attempts = 0;
    const { user, backend } = await serviceDetailAt(NAME, {
      "POST /api/services/verify": () => json(200, { success: true, output: "" }),
      [`PUT /api/services/${NAME}/config`]: () => {
        attempts += 1;
        return attempts === 1
          ? problem(403, "elevation_required", "Confirm it's you to continue.")
          : json(200, { success: true, message: `Configuration updated for ${NAME}.`, service: NAME });
      },
      "POST /api/auth/elevate": () => json(200, { elevated_until: "2999-01-01T00:00:00+00:00" }),
    });
    const textarea = await screen.findByLabelText(`Unit file for ${NAME}`, { exact: true });
    await user.type(textarea, "# edited");
    await user.click(screen.getByRole("button", { name: "Save unit file" }));

    const elevate = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(elevate).getByRole("textbox"), "123456");
    await user.click(within(elevate).getByRole("button", { name: "Confirm" }));

    expect(await screen.findByText("Saved. systemd-analyze found no problems.")).toBeInTheDocument();
    // Refused unelevated, then retried once after "Confirm it's you": two attempts, one save.
    expect(backend.callsTo(`PUT /api/services/${NAME}/config`)).toHaveLength(2);
  });

  it("has no accessibility violations after a failed verify", async () => {
    const { user } = await serviceDetailAt(NAME, {
      "POST /api/services/verify": () => json(200, { success: false, output: "Service has no ExecStart= setting. Refusing.\n" }),
    });
    const textarea = await screen.findByLabelText(`Unit file for ${NAME}`, { exact: true });
    await user.type(textarea, "# edited");
    await user.click(screen.getByRole("button", { name: "Save unit file" }));
    await screen.findByText(/systemd-analyze rejected the unit/);
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

describe("a unit WASM did not create", () => {
  const FOREIGN_NAME = "postgresql";
  const ALL_SERVICES: ServiceList["services"] = [
    SERVICE,
    {
      name: FOREIGN_NAME,
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

  async function foreignAt() {
    const backend = fakeBackend({
      ...signedInRoutes(),
      [`GET /api/services/${FOREIGN_NAME}`]: () => problem(404, "not_found", `Service not found: ${FOREIGN_NAME}`),
      "GET /api/services": (call) =>
        call.search.get("wasm_only") === "false"
          ? json(200, { services: ALL_SERVICES, total: ALL_SERVICES.length })
          : json(200, { services: [SERVICE], total: 1 }),
    });
    const harness = renderConsole(`/services/${FOREIGN_NAME}`);
    await screen.findByRole("heading", { level: 1, name: FOREIGN_NAME });
    return { ...harness, backend };
  }

  it("says WASM did not create it and offers nothing destructive", async () => {
    await foreignAt();
    expect(await screen.findByText("WASM did not create this unit")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Restart/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Unit file for/)).not.toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    await foreignAt();
    await screen.findByText("WASM did not create this unit");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

describe("a name nothing on the machine answers to", () => {
  it("says so plainly, once the all-units listing agrees nothing exists", async () => {
    fakeBackend({
      ...signedInRoutes(),
      "GET /api/services/ghost": () => problem(404, "not_found", "Service not found: ghost"),
      "GET /api/services": () => json(200, { services: [SERVICE], total: 1 }),
    });
    renderConsole("/services/ghost");
    expect(await screen.findByText("No service by this name")).toBeInTheDocument();
  });
});

import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { FakeEventSource, MACHINE, fakeBackend, json, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

// uPlot draws on a canvas jsdom does not have; the charts' contract is their summary.
vi.mock("uplot", () => ({
  default: vi.fn(function () {
    return { destroy: vi.fn(), setData: vi.fn(), setSize: vi.fn(), redraw: vi.fn() };
  }),
}));

const NOW = Math.floor(Date.now() / 1000);

const DEPLOYS = [
  {
    id: 12,
    domain: "admin.example.com",
    status: "failed",
    triggered_by: "panel",
    git_commit: "c07d5e3",
    git_branch: "main",
    started_at: "2026-09-25T19:20:35",
    finished_at: "2026-09-25T19:21:05",
    duration_s: 30,
    error: "npm ERR! code ELIFECYCLE\nnpm ERR! errno 1",
    has_log: true,
  },
  {
    id: 11,
    domain: "shop.example.com",
    status: "success",
    triggered_by: "cli",
    git_commit: "9f2c41a",
    git_branch: "main",
    started_at: "2026-09-25T18:00:00",
    finished_at: "2026-09-25T18:00:21",
    duration_s: 21.4,
    error: null,
    has_log: true,
  },
];

function overviewRoutes(extra: Record<string, RouteHandler> = {}): Record<string, RouteHandler> {
  const series = (value: number) => () => json(200, { metric: "m", window: "1h", points: [[NOW - 60, value], [NOW, value]] });
  return {
    ...signedInRoutes(),
    "GET /api/deployments": () => json(200, { items: DEPLOYS, total: DEPLOYS.length, next_before_id: null }),
    "GET /api/certs": () =>
      json(200, {
        total: 1,
        certificates: [{ domain: "shop.example.com", domains: ["shop.example.com"], days_remaining: 12, expires_on: "2026-10-07", auto_renew: true }],
      }),
    "GET /api/monitor/observations": () => json(200, { observations: [], count: 0, stats: null }),
    "GET /api/metrics/cpu.percent": series(12),
    "GET /api/metrics/mem.used_bytes": series(4e9),
    "GET /api/metrics/mem.total_bytes": series(8e9),
    "GET /api/metrics/net.rx_bytes_s": series(1200),
    "GET /api/metrics/net.tx_bytes_s": series(800),
    "GET /api/metrics/disk.used_bytes": series(6e10),
    "GET /api/metrics/disk.total_bytes": series(1e11),
    ...extra,
  };
}

async function overview(extra?: Record<string, RouteHandler>, path = "/") {
  const backend = fakeBackend(overviewRoutes(extra));
  const harness = renderConsole(path);
  await screen.findByRole("heading", { level: 1, name: "Overview" });
  return { ...harness, backend };
}

describe("the overview", () => {
  it("puts a failed deploy under Needs attention, verbatim, linking to the app and its diagnosis", async () => {
    await overview();
    const attention = screen.getByRole("region", { name: /Needs attention/ });
    const app = await within(attention).findByRole("link", { name: "admin.example.com" });
    expect(app).toHaveAttribute("href", "/apps/admin.example.com");
    expect(within(attention).getByText("Last deploy failed")).toBeInTheDocument();
    expect(within(attention).getByText("npm ERR! code ELIFECYCLE")).toBeInTheDocument();
    expect(within(attention).getByRole("link", { name: "Diagnose admin.example.com" })).toHaveAttribute(
      "href",
      "/apps/admin.example.com/diagnose",
    );
    expect(within(attention).getByRole("link", { name: /View log/ })).toHaveAttribute("href", "/apps/admin.example.com/deployments/12");
  });

  it("also names the app whose state is a problem, and the expiring certificate", async () => {
    await overview();
    const attention = screen.getByRole("region", { name: /Needs attention/ });
    await within(attention).findByText("Certificate expires in 12 days");
    // The failed unit of the fake machine is already named by admin.example.com's state.
    expect(within(attention).queryByText(/failed WASM unit/)).not.toBeInTheDocument();
    expect(within(attention).getByText("The service has failed")).toBeInTheDocument();
  });

  it("says in one line that nothing needs attention on a healthy machine", async () => {
    await overview({
      "GET /api/apps": () => json(200, { total: 1, apps: [{ domain: "shop.example.com", name: "shop", status: "running", active: true, enabled: true, layout: "inplace" }] }),
      "GET /api/deployments": () => json(200, { items: [DEPLOYS[1]], total: 1, next_before_id: null }),
      "GET /api/certs": () => json(200, { total: 0, certificates: [] }),
      "GET /api/system/machine": () => json(200, { ...MACHINE, units: { running: 1, failed: 0, stopped: 0 } }),
    });
    expect(await screen.findByText("Nothing needs attention.")).toBeInTheDocument();
  });

  it("notes a source it could not check instead of skipping it silently", async () => {
    await overview({
      "GET /api/certs": () => json(500, { error: "internal", detail: "certbot: command not found", hint: null, fields: null }),
    });
    expect(await screen.findByText("certbot: command not found")).toBeInTheDocument();
    expect(screen.getByText(/Certificates could not be checked/)).toBeInTheDocument();
  });

  it("lists every application with its state and last deploy", async () => {
    await overview();
    const table = await screen.findByRole("region", { name: "Applications on this machine" });
    expect(await within(table).findByRole("link", { name: "shop.example.com" })).toBeInTheDocument();
    expect(within(table).getByText("Failed")).toBeInTheDocument();
  });

  it("keeps recent deployments live: a job ending refreshes them", async () => {
    const { backend } = await overview();
    const recent = screen.getByRole("region", { name: "Recent deployments, newest first" });
    await within(recent).findByText("9f2c41a");
    const before = backend.callsTo("GET /api/deployments").length;
    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().emit("job", { id: "j1", type: "update", status: "completed", metadata: { domain: "shop.example.com" } });
    });
    await waitFor(() => {
      expect(backend.callsTo("GET /api/deployments").length).toBeGreaterThan(before);
    });
  });

  it("draws the machine's history and keeps the chosen range in the URL", async () => {
    const { user, location } = await overview();
    expect(await screen.findByRole("img", { name: /^CPU, last hour\. CPU: latest 12%/ })).toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "24h" }));
    await waitFor(() => {
      expect(location().search).toEqual({ window: "24h" });
    });
  });

  it("opens with the range the URL names", async () => {
    await overview(undefined, "/?window=30d");
    expect(screen.getByRole("radio", { name: "30d" })).toHaveAttribute("aria-checked", "true");
  });

  it("has no accessibility violations", async () => {
    await overview();
    await screen.findByRole("link", { name: "admin.example.com" });
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

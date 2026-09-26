import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderConsole } from "../../test/console";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

// uPlot draws on a canvas jsdom does not have; the overview's charts are not this test's concern.
vi.mock("uplot", () => ({
  default: vi.fn(function () {
    return { destroy: vi.fn(), setData: vi.fn(), setSize: vi.fn(), redraw: vi.fn() };
  }),
}));

const NOW = Math.floor(Date.now() / 1000);

const DEPLOYS = [
  {
    id: 12,
    domain: "shop.example.com",
    status: "success",
    triggered_by: "webhook",
    git_commit: "9f2c41a",
    git_branch: "main",
    started_at: "2026-09-25T18:00:00",
    finished_at: "2026-09-25T18:00:21",
    duration_s: 21.4,
    error: null,
    has_log: true,
    commit_message: "Fix checkout total rounding",
  },
  {
    id: 11,
    domain: "admin.example.com",
    status: "success",
    triggered_by: "cli",
    git_commit: "c07d5e3",
    git_branch: "main",
    started_at: "2026-09-25T17:00:00",
    finished_at: "2026-09-25T17:00:19",
    duration_s: 19,
    error: null,
    has_log: true,
    commit_message: null,
  },
];

function routes(extra: Record<string, RouteHandler> = {}): Record<string, RouteHandler> {
  const series = (value: number) => () => json(200, { metric: "m", window: "1h", points: [[NOW - 60, value], [NOW, value]] });
  return {
    ...signedInRoutes(),
    "GET /api/deployments": () => json(200, { items: DEPLOYS, total: DEPLOYS.length, next_before_id: null }),
    "GET /api/certs": () => json(200, { certificates: [], total: 0 }),
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

async function overview(extra?: Record<string, RouteHandler>) {
  const backend = fakeBackend(routes(extra));
  const harness = renderConsole("/");
  await screen.findByRole("heading", { level: 1, name: "Overview" });
  return { ...harness, backend };
}

describe("recent deployments, on the overview", () => {
  it("shows each deploy's commit message beside its hash, truncated but reachable in full", async () => {
    await overview();
    const table = await screen.findByRole("region", { name: "Recent deployments, newest first" });
    await within(table).findByRole("link", { name: /9f2c41a/ });
    expect(within(table).getByTitle("Fix checkout total rounding")).toBeInTheDocument();
  });

  it("shows nothing extra for a deploy with no commit message", async () => {
    await overview();
    const table = await screen.findByRole("region", { name: "Recent deployments, newest first" });
    const row = (await within(table).findByRole("link", { name: /c07d5e3/ })).closest("tr");
    expect(row).not.toBeNull();
    // The row's own commit hash is there; nothing else stands in for the missing message.
    expect(within(row as HTMLElement).queryByTitle(/./)).not.toBeInTheDocument();
  });
});

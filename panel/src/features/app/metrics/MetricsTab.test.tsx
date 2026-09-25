import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../../test/axe";
import { renderConsole } from "../../../test/console";
import { fakeBackend, json } from "../../../test/fakes";
import type { RouteHandler } from "../../../test/fakes";
import { TAB_DOMAIN, appRoutes } from "../testRoutes";
import { clip, momentWords, sentence, summarise } from "./ranges";

// uPlot draws on a canvas jsdom does not have; the charts' contract is their summary.
vi.mock("uplot", () => ({
  default: vi.fn(function () {
    return { destroy: vi.fn(), setData: vi.fn(), setSize: vi.fn(), redraw: vi.fn() };
  }),
}));

const NOW = Math.floor(Date.now() / 1000);

function localIso(seconds: number): string {
  const date = new Date(seconds * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

const series =
  (values: readonly (readonly [number, number])[]): RouteHandler =>
  (call) =>
    json(200, { metric: call.path.split("/").pop(), window: call.search.get("window") ?? "1h", points: values });

async function metricsAt(path = `/apps/${TAB_DOMAIN}/metrics`, app: Record<string, unknown> = { memory_max_mb: 512 }) {
  const backend = fakeBackend(
    appRoutes(app, {
      [`GET /api/metrics/app.${TAB_DOMAIN}.cpu.percent`]: series([
        [NOW - 9 * 86_400, 9],
        [NOW - 3 * 86_400, 30],
        [NOW - 3_000, 4],
        [NOW - 60, 2],
      ]),
      [`GET /api/metrics/app.${TAB_DOMAIN}.mem.bytes`]: series([
        [NOW - 3_000, 200 * 1024 * 1024],
        [NOW - 60, 300 * 1024 * 1024],
      ]),
      "GET /api/deployments": () =>
        json(200, {
          items: [
            { id: 25, domain: TAB_DOMAIN, status: "success", triggered_by: "webhook", git_commit: "2a8b7c4", git_branch: "main", started_at: localIso(NOW - 1_800), finished_at: null, duration_s: 47, error: null, has_log: true },
            { id: 9, domain: TAB_DOMAIN, status: "failed", triggered_by: "cli", git_commit: "d08e4f7", git_branch: "main", started_at: localIso(NOW - 20 * 86_400), finished_at: null, duration_s: 30, error: "x", has_log: true },
          ],
          total: 2,
          next_before_id: null,
        }),
    }),
  );
  const harness = renderConsole(path);
  await screen.findByRole("heading", { level: 1, name: TAB_DOMAIN });
  return { ...harness, backend };
}

describe("the metrics tab's words", () => {
  it("summarises a series as average, peak with its time, and latest", () => {
    const summary = summarise([
      [1000, 2],
      [2000, 10],
      [3000, 3],
    ]);
    expect(summary).toEqual({ latest: 3, average: 5, peak: 10, peakAt: 2000 });
    if (summary === null) throw new Error("no summary");
    expect(sentence(summary, "24h", (value) => `${String(value)}%`, 150)).toBe(`Average 5%, peak 10% at ${momentWords(2000, "24h")}, latest 3%, limit 150%.`);
  });

  it("cuts the thirty-day read to a week for the 7d range", () => {
    const points = [
      [NOW - 9 * 86_400, 1],
      [NOW - 86_400, 2],
    ] as const;
    expect(clip(points, "7d", NOW)).toEqual([[NOW - 86_400, 2]]);
    expect(clip(points, "30d", NOW)).toHaveLength(2);
  });
});

// Whole-console renders: generous under a loaded machine or a slow CI runner.
describe("the metrics tab", { timeout: 20_000 }, () => {
  it("charts CPU and memory with a sentence each, and lists the deploys in the range", async () => {
    await metricsAt();
    expect(await screen.findByRole("img", { name: /^CPU, last 24 hours/ })).toBeInTheDocument();
    const memory = await screen.findByRole("img", { name: /^Memory, last 24 hours/ });
    expect(memory).toBeInTheDocument();
    // The limit is said even when it is not drawn.
    expect(await screen.findByText(/Average 250 MB, peak 300 MB at .*, latest 300 MB, limit 512 MB\./)).toBeInTheDocument();
    const deploys = screen.getByRole("region", { name: "Deploys in this range" });
    expect(within(deploys).getByRole("link", { name: /^Deploy 25, succeeded/ })).toHaveAttribute("href", `/apps/${TAB_DOMAIN}/deployments/25`);
    expect(within(deploys).queryByRole("link", { name: /^Deploy 9/ })).not.toBeInTheDocument();
  });

  it("keeps the range in the URL and reads the thirty-day tier for a week", async () => {
    const { user, location, backend } = await metricsAt();
    await screen.findByRole("img", { name: /^CPU, last 24 hours/ });
    await user.click(screen.getByRole("radio", { name: "7d" }));
    await waitFor(() => {
      expect(location().search).toEqual({ range: "7d" });
    });
    expect(await screen.findByRole("img", { name: /^CPU, last 7 days/ })).toBeInTheDocument();
    const cpu = backend.callsTo(`GET /api/metrics/app.${TAB_DOMAIN}.cpu.percent`).at(-1);
    expect(cpu?.search.get("window")).toBe("30d");
    // The nine-day-old reading is cut from the week.
    expect(await screen.findByText(/peak 30% at/)).toBeInTheDocument();
  });

  it("opens on the range the URL names", async () => {
    await metricsAt(`/apps/${TAB_DOMAIN}/metrics?range=30d`);
    expect(await screen.findByRole("img", { name: /^CPU, last 30 days/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "30d" })).toBeChecked();
  });

  it("says a static site has nothing to measure", async () => {
    await metricsAt(undefined, { status: "static", active: false, port: null });
    expect(await screen.findByRole("heading", { name: "A static site has no process to measure" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    await metricsAt();
    await screen.findByRole("img", { name: /^Memory, last 24 hours/ });
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

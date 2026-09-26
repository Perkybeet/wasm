import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../../test/axe";
import { renderConsole } from "../../../test/console";
import { FakeWebSocket, fakeBackend, json, problem } from "../../../test/fakes";
import type { RouteHandler } from "../../../test/fakes";
import { TAB_DOMAIN, appRoutes } from "../testRoutes";

const FAILED = {
  id: 20,
  domain: TAB_DOMAIN,
  status: "failed",
  triggered_by: "webhook",
  git_commit: "a94c0e2",
  git_branch: "main",
  started_at: "2026-09-25T18:00:00",
  finished_at: "2026-09-25T18:00:40",
  duration_s: 40,
  error: "npm run build exited with status 1\n./app/checkout/page.tsx:42:7\nType error: Property 'total' does not exist on type 'Order'.",
  has_log: true,
};

const FAILED_LOG = [
  "[2026-09-25 18:00:00] [1/9] 📥 Fetching source into a new release...",
  "[2026-09-25 18:00:02]       → HEAD is now at a94c0e2",
  "[2026-09-25 18:00:03] [2/9] 📦 Installing dependencies...",
  "[2026-09-25 18:00:15] added 812 packages, and audited 813 packages in 12s",
  "[2026-09-25 18:00:16] [3/9] 🔨 Building application...",
  "[2026-09-25 18:00:39] Type error: Property 'total' does not exist on type 'Order'.",
  "",
].join("\n");

async function pageAt(
  id: number | string,
  extra: Record<string, RouteHandler> = {},
  app: Record<string, unknown> = {},
) {
  const backend = fakeBackend(
    appRoutes(app, {
      "GET /api/deployments": () => json(200, { items: [FAILED], total: 1, next_before_id: null }),
      [`GET /api/deployments/${String(FAILED.id)}`]: () => json(200, FAILED),
      [`GET /api/deployments/${String(FAILED.id)}/log`]: () => json(200, { content: FAILED_LOG, truncated: false, missing_reason: null }),
      "GET /api/jobs": () => json(200, { jobs: [], total: 0, active: 0 }),
      ...extra,
    }),
  );
  const harness = renderConsole(`/apps/${TAB_DOMAIN}/deployments/${String(id)}`);
  await screen.findByRole("heading", { level: 1, name: TAB_DOMAIN });
  return { ...harness, backend };
}

function phaseStates(): Record<string, string | null> {
  const list = screen.getByRole("list", { name: "Deploy phases" });
  const entries: [string, string | null][] = within(list)
    .getAllByRole("listitem")
    .map((item) => [item.dataset["phase"] ?? "", item.dataset["state"] ?? null]);
  return Object.fromEntries(entries);
}

// Whole-console renders: generous under a loaded machine or a slow CI runner.
describe("a deployment's page", { timeout: 20_000 }, () => {
  it("says where a failed deploy stopped, the fix above and the error verbatim below", async () => {
    await pageAt(20);
    expect(await screen.findByRole("heading", { level: 2, name: "Deployment 20" })).toBeInTheDocument();
    expect(await screen.findByText("The deploy failed while building")).toBeInTheDocument();
    expect(screen.getByText(/The build's own output is in the log below/)).toBeInTheDocument();
    const verbatim = screen.getByText(/Type error: Property 'total' does not exist on type 'Order'\.$/, { selector: "pre" });
    expect(verbatim.textContent).toBe(FAILED.error);
    await waitFor(() => {
      expect(phaseStates()).toEqual({ fetch: "done", install: "done", build: "failed", activate: "unrecorded", health: "unrecorded" });
    });
    const phases = screen.getByRole("list", { name: "Deploy phases" });
    expect(within(phases).getAllByText("Not reached")).toHaveLength(2);
    expect(within(phases).getByText("13s")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Diagnose this app" })).toHaveAttribute("href", `/apps/${TAB_DOMAIN}/diagnose`);
  });

  it("shows the build log with its times apart from the verbatim text", async () => {
    // The virtualizer sizes its window from offsetHeight, which jsdom reports as 0.
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(400);
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(800);
    await pageAt(20);
    const log = await screen.findByRole("region", { name: `Build log of deployment 20 of ${TAB_DOMAIN}` });
    expect(await within(log).findByText("added 812 packages, and audited 813 packages in 12s")).toBeInTheDocument();
    expect(within(log).getAllByText("18:00:15")).not.toHaveLength(0);
  });

  it("follows a running deploy: its job over the WebSocket, its log as it grows, until it ends", async () => {
    vi.stubGlobal("WebSocket", FakeWebSocket);
    // Linked by the deployment's own job_id, not by matching it against the active jobs list.
    const running = { ...FAILED, id: 31, status: "running", finished_at: null, duration_s: null, error: null, job_id: "8048ab18" };
    let status = "running";
    let content = "[2026-09-25 18:00:03]       → Running: npm ci\n";
    const job = {
      id: "8048ab18",
      type: "update",
      name: `Update ${TAB_DOMAIN}`,
      description: "",
      status: "running",
      progress: 20,
      total_steps: 100,
      current_step: "Installing dependencies",
      created_at: "2026-09-25T18:00:00",
      started_at: "2026-09-25T18:00:00",
      logs: [
        { timestamp: "2026-09-25T18:00:01", level: "info", message: "Pulling latest changes", step: 20 },
        { timestamp: "2026-09-25T18:00:03", level: "info", message: "Installing dependencies", step: 60 },
      ],
      metadata: { domain: TAB_DOMAIN },
    };
    const { queryClient } = await pageAt(31, {
      "GET /api/deployments/31": () => json(200, { ...running, status }),
      "GET /api/deployments/31/log": () => json(200, { content, truncated: false, missing_reason: null }),
      "POST /api/auth/ws-ticket": () => json(200, { ticket: "t1" }),
    });

    await waitFor(() => {
      expect(FakeWebSocket.instances.some((socket) => socket.url.includes("/ws/jobs/8048ab18"))).toBe(true);
    });
    const socket = FakeWebSocket.instances.find((candidate) => candidate.url.includes("/ws/jobs/8048ab18"));
    act(() => {
      socket?.open();
      socket?.frame({ type: "connected", job });
    });
    await waitFor(() => {
      expect(phaseStates()).toMatchObject({ fetch: "done", install: "running", build: "pending" });
    });

    content += "[2026-09-25 18:00:20]       → Running: npm run build\n";
    act(() => {
      socket?.frame({
        type: "update",
        job: { ...job, logs: [...job.logs, { timestamp: "2026-09-25T18:00:20", level: "info", message: "Building", step: 80 }] },
      });
    });
    await waitFor(
      () => {
        expect(phaseStates()).toMatchObject({ install: "done", build: "running" });
      },
      { timeout: 3_000 },
    );

    status = "success";
    act(() => {
      socket?.frame({ type: "finished", job: { ...job, status: "completed" } });
    });
    await queryClient.invalidateQueries({ queryKey: ["deployments"] });
    expect(await screen.findByText("Succeeded")).toBeInTheDocument();
  });

  it("redeploys and opens the new deploy once it is recorded, linked by the job's own id", async () => {
    // Not "the newest deployment": one that arrived after the click but for a different job
    // (someone else's update, or a push) must not be mistaken for this one.
    let queued = false;
    const { user, location } = await pageAt(20, {
      "GET /api/deployments": () =>
        json(200, {
          items: queued
            ? [
                { ...FAILED, id: 22, job_id: "someone-elses-job", status: "success", error: null },
                { ...FAILED, id: 21, job_id: "0badcafe", status: "running", error: null, finished_at: null },
                { ...FAILED, id: 20 },
              ]
            : [{ ...FAILED, id: 20 }],
          total: queued ? 3 : 1,
          next_before_id: null,
        }),
      "POST /api/jobs/update": () => {
        queued = true;
        return json(202, {
          message: "Update job created",
          job: { id: "0badcafe", type: "update", name: "", description: "", status: "pending", progress: 0, total_steps: 100, current_step: "", created_at: "2026-09-25T19:00:00", logs: [], metadata: { domain: TAB_DOMAIN } },
        });
      },
      "GET /api/jobs/0badcafe": () => json(200, { id: "0badcafe", type: "update", name: "", description: "", status: "running", progress: 0, total_steps: 100, current_step: "", created_at: "2026-09-25T19:00:00", logs: [], metadata: { domain: TAB_DOMAIN } }),
      "GET /api/deployments/21": () => json(200, { ...FAILED, id: 21, status: "running", error: null, finished_at: null, job_id: "0badcafe" }),
      "GET /api/deployments/21/log": () => json(200, { content: "", truncated: false, missing_reason: null }),
    });
    await user.click(await screen.findByRole("button", { name: "Redeploy" }));
    await waitFor(
      () => {
        expect(location().pathname).toBe(`/apps/${TAB_DOMAIN}/deployments/21`);
      },
      { timeout: 4_000 },
    );
    expect(await screen.findByRole("heading", { level: 2, name: "Deployment 21" })).toBeInTheDocument();
  });

  it("says the redeploy was queued, without a deploy to open, when the job ends without recording one", async () => {
    const { user } = await pageAt(20, {
      "POST /api/jobs/update": () =>
        json(202, {
          message: "Update job created",
          job: { id: "cafebabe", type: "update", name: "", description: "", status: "pending", progress: 0, total_steps: 100, current_step: "", created_at: "2026-09-25T19:00:00", logs: [], metadata: { domain: TAB_DOMAIN } },
        }),
      "GET /api/jobs/cafebabe": () =>
        json(200, { id: "cafebabe", type: "update", name: "", description: "", status: "completed", progress: 100, total_steps: 100, current_step: "", created_at: "2026-09-25T19:00:00", completed_at: "2026-09-25T19:00:01", logs: [], metadata: { domain: TAB_DOMAIN } }),
    });
    await user.click(await screen.findByRole("button", { name: "Redeploy" }));
    // The job is only found "completed" on jobQuery's own 3s follow poll, not before.
    expect(await screen.findByText(/The update was queued but no deploy has been recorded yet/, {}, { timeout: 4_000 })).toBeInTheDocument();
  });

  it("shows the commit's message beside its hash and branch", async () => {
    await pageAt(20, {
      [`GET /api/deployments/${String(FAILED.id)}`]: () => json(200, { ...FAILED, commit_message: "Fix checkout total rounding" }),
    });
    expect(await screen.findByText("Fix checkout total rounding")).toBeInTheDocument();
  });

  it("says nothing ran a build for it when the deploy has no job (a CLI deploy)", async () => {
    // FAILED carries no job_id: the command line ran it, nothing queued it.
    await pageAt(20);
    await screen.findByText("The deploy failed while building");
    // Its own captured log is still read and shown; only a job's WebSocket/log never is.
    expect(await screen.findByText(/Type error: Property 'total' does not exist on type 'Order'\./)).toBeInTheDocument();
  });

  it("offers to roll back to the deploy's own release, matched by release_id, not by commit or time", async () => {
    await pageAt(
      20,
      {
        [`GET /api/deployments/${String(FAILED.id)}`]: () => json(200, { ...FAILED, release_id: "20260916-182823-a94c0e2" }),
        [`GET /api/apps/${TAB_DOMAIN}/releases`]: () =>
          json(200, {
            domain: TAB_DOMAIN,
            items: [
              {
                id: "20260925-184247-2a8b7c4",
                // A different release built from the very same commit: matching by commit would
                // pick this one - the one serving - instead of the deploy's own.
                commit: "a94c0e2",
                created_at: "2026-09-25T18:42:47+00:00",
                activated_at: "2026-09-25T18:43:30+00:00",
                status: "active",
                active: true,
                on_disk: true,
              },
              {
                id: "20260916-182823-a94c0e2",
                commit: "a94c0e2",
                created_at: "2026-09-16T18:28:23+00:00",
                activated_at: null,
                status: "superseded",
                active: false,
                on_disk: true,
              },
            ],
            total: 2,
          }),
      },
      { layout: "releases" },
    );
    expect(await screen.findByRole("button", { name: "Roll back to this" })).toBeInTheDocument();
  });

  it("offers a plain rollback when the deploy built no release still on disk", async () => {
    await pageAt(
      20,
      {
        [`GET /api/apps/${TAB_DOMAIN}/releases`]: () =>
          json(200, {
            domain: TAB_DOMAIN,
            items: [
              { id: "20260925-184247-2a8b7c4", commit: "2a8b7c4", created_at: "2026-09-25T18:42:47+00:00", activated_at: "2026-09-25T18:43:30+00:00", status: "active", active: true, on_disk: true },
            ],
            total: 1,
          }),
      },
      { layout: "releases" },
    );
    expect(await screen.findByRole("button", { name: "Roll back" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Roll back to this" })).not.toBeInTheDocument();
  });

  it("says plainly when there is no such deployment", async () => {
    await pageAt(99, { "GET /api/deployments/99": () => problem(404, "not_found", "Deployment not found: 99") });
    expect(await screen.findByRole("heading", { name: "No deployment 99" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    await pageAt(20);
    await screen.findByText("The deploy failed while building");
    await screen.findByRole("region", { name: `Build log of deployment 20 of ${TAB_DOMAIN}` });
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

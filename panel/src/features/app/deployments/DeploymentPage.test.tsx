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

/** A queued job as the API answers it, for the actions' 202s. */
const JOB = {
  id: "0badcafe",
  type: "update",
  name: "",
  description: "",
  status: "pending",
  progress: 0,
  total_steps: 100,
  current_step: "",
  created_at: "2026-09-25T19:00:00",
  logs: [],
  metadata: { domain: TAB_DOMAIN },
};

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

  it("rebuilds the deploy's commit after saying what that does, and opens the new deploy linked by the job's own id", async () => {
    // Not "the newest deployment": one that arrived after the click but for a different job
    // (someone else's update, or a push) must not be mistaken for this one.
    let queued = false;
    const { user, location, backend } = await pageAt(
      20,
      {
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
        [`POST /api/apps/${TAB_DOMAIN}/deployments/20/rebuild`]: () => {
          queued = true;
          return json(202, { job_id: "0badcafe", status: "pending", message: "Rebuild queued", job: { ...JOB, id: "0badcafe" } });
        },
        "GET /api/jobs/0badcafe": () => json(200, { ...JOB, id: "0badcafe", status: "running" }),
        "GET /api/deployments/21": () => json(200, { ...FAILED, id: 21, status: "running", error: null, finished_at: null, job_id: "0badcafe" }),
        "GET /api/deployments/21/log": () => json(200, { content: "", truncated: false, missing_reason: null }),
      },
      { layout: "releases" },
    );
    await user.click(await screen.findByRole("button", { name: "Rebuild this commit" }));
    const dialog = await screen.findByRole("dialog", { name: "Rebuild commit a94c0e2?" });
    expect(within(dialog).getByText(/is still on disk and is not the one serving, it is activated in seconds/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Rebuild" }));
    expect(backend.calls.some((call) => call.method === "POST" && call.path === `/api/apps/${TAB_DOMAIN}/deployments/20/rebuild`)).toBe(true);
    // Never the plain update the header's Update runs.
    expect(backend.calls.some((call) => call.path === "/api/jobs/update")).toBe(false);
    await waitFor(
      () => {
        expect(location().pathname).toBe(`/apps/${TAB_DOMAIN}/deployments/21`);
      },
      { timeout: 4_000 },
    );
    expect(await screen.findByRole("heading", { level: 2, name: "Deployment 21" })).toBeInTheDocument();
  });

  it("explains an in-place rebuild: the checkout goes to the commit and the branch is followed again next time", async () => {
    const { user } = await pageAt(20);
    await user.click(await screen.findByRole("button", { name: "Rebuild this commit" }));
    const dialog = await screen.findByRole("dialog", { name: "Rebuild commit a94c0e2?" });
    expect(within(dialog).getByText(/the checkout is put on a94c0e2.*The next update follows main again\./)).toBeInTheDocument();
  });

  it("keeps a failed rebuild's error on screen, in the job's own words", async () => {
    const { user } = await pageAt(20, {
      [`POST /api/apps/${TAB_DOMAIN}/deployments/20/rebuild`]: () =>
        json(202, { job_id: "cafebabe", status: "pending", message: "Rebuild queued", job: { ...JOB, id: "cafebabe" } }),
      "GET /api/jobs/cafebabe": () =>
        json(200, { ...JOB, id: "cafebabe", status: "failed", error: "Commit a94c0e2 does not exist in the repository" }),
    });
    await user.click(await screen.findByRole("button", { name: "Rebuild this commit" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Rebuild commit a94c0e2?" })).getByRole("button", { name: "Rebuild" }));
    const title = await screen.findByText("The rebuild failed", {}, { timeout: 4_000 });
    const alert = title.closest<HTMLElement>("[role='alert']");
    if (alert === null) throw new Error("the failure is not announced");
    expect(within(alert).getByText("Commit a94c0e2 does not exist in the repository")).toBeInTheDocument();
  });

  it("says a rebuild that recorded no deploy of its own finished", async () => {
    const { user } = await pageAt(20, {
      [`POST /api/apps/${TAB_DOMAIN}/deployments/20/rebuild`]: () =>
        json(202, { job_id: "cafebabe", status: "pending", message: "Rebuild queued", job: { ...JOB, id: "cafebabe" } }),
      "GET /api/jobs/cafebabe": () => json(200, { ...JOB, id: "cafebabe", status: "completed", completed_at: "2026-09-25T19:00:01" }),
    });
    await user.click(await screen.findByRole("button", { name: "Rebuild this commit" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Rebuild commit a94c0e2?" })).getByRole("button", { name: "Rebuild" }));
    // The job is only found "completed" on jobQuery's own 3s follow poll, not before.
    expect(await screen.findByText("Rebuilt commit a94c0e2: it is live.", {}, { timeout: 4_000 })).toBeInTheDocument();
  });

  it("offers no rebuild for a deploy whose source is not git", async () => {
    await pageAt(20, { [`GET /api/deployments/${String(FAILED.id)}`]: () => json(200, { ...FAILED, git_commit: null }) });
    await screen.findByText("The deploy failed while building");
    expect(screen.queryByRole("button", { name: "Rebuild this commit" })).not.toBeInTheDocument();
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

  it("rolls back to this deployment when the backend says it can, through its own endpoint", async () => {
    const SERVED = { ...FAILED, status: "success", error: null, release_id: "20260916-182823-a94c0e2", rollback_available: true, rollback_unavailable_reason: null };
    const { user, backend } = await pageAt(
      20,
      {
        [`GET /api/deployments/${String(FAILED.id)}`]: () => json(200, SERVED),
        [`POST /api/apps/${TAB_DOMAIN}/deployments/20/rollback`]: () =>
          json(202, { job_id: "5ca1ab1e", status: "pending", message: "Rollback queued", job: { ...JOB, id: "5ca1ab1e", type: "restore" } }),
        "GET /api/jobs/5ca1ab1e": () => json(200, { ...JOB, id: "5ca1ab1e", type: "restore", status: "completed", completed_at: "2026-09-25T19:00:01" }),
      },
      { layout: "releases" },
    );
    await user.click(await screen.findByRole("button", { name: "Roll back to this" }));
    const dialog = await screen.findByRole("dialog", { name: "Roll back to deployment 20?" });
    expect(within(dialog).getByText(/Release 20260916-182823-a94c0e2 is activated in seconds/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));
    expect(backend.calls.some((call) => call.method === "POST" && call.path === `/api/apps/${TAB_DOMAIN}/deployments/20/rollback`)).toBe(true);
    expect(await screen.findByText("Rolled back to deployment 20.", {}, { timeout: 4_000 })).toBeInTheDocument();
  });

  it("says an in-place rollback restores the deployment's snapshot, after a backup of the current state", async () => {
    const { user } = await pageAt(20, {
      [`GET /api/deployments/${String(FAILED.id)}`]: () =>
        json(200, { ...FAILED, status: "success", error: null, snapshot_backup: "app_20260920_101500", rollback_available: true }),
    });
    await user.click(await screen.findByRole("button", { name: "Roll back to this" }));
    const dialog = await screen.findByRole("dialog", { name: "Roll back to deployment 20?" });
    expect(within(dialog).getByText(/restored from backup app_20260920_101500.*A backup of the current state is taken first/)).toBeInTheDocument();
  });

  it("says why a deployment cannot be gone back to, and offers the other versions instead", async () => {
    const { user } = await pageAt(
      20,
      {
        [`GET /api/deployments/${String(FAILED.id)}`]: () =>
          json(200, { ...FAILED, rollback_available: false, rollback_unavailable_reason: "Deployment 20 did not finish serving anything to go back to" }),
        [`GET /api/apps/${TAB_DOMAIN}/releases`]: () =>
          json(200, {
            domain: TAB_DOMAIN,
            items: [
              { id: "20260925-184247-2a8b7c4", commit: "2a8b7c4", created_at: "2026-09-25T18:42:47+00:00", activated_at: "2026-09-25T18:43:30+00:00", status: "active", active: true, on_disk: true },
              { id: "20260916-182823-9f2c41a", commit: "9f2c41a", created_at: "2026-09-16T18:28:23+00:00", activated_at: null, status: "superseded", active: false, on_disk: true },
            ],
            total: 2,
          }),
      },
      { layout: "releases" },
    );
    expect(await screen.findByText("Can't roll back to this deployment: Deployment 20 did not finish serving anything to go back to.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Roll back to this" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Roll back…" }));
    const chooser = await screen.findByRole("dialog", { name: `Roll back ${TAB_DOMAIN}` });
    expect(await within(chooser).findByRole("radio", { name: /20260916-182823-9f2c41a/ })).toBeInTheDocument();
  });

  it("says Health does not apply to a static site, instead of that it is missing from the log", async () => {
    await pageAt(
      20,
      {
        [`GET /api/deployments/${String(FAILED.id)}`]: () => json(200, { ...FAILED, status: "success", error: null }),
        [`GET /api/deployments/${String(FAILED.id)}/log`]: () =>
          json(200, {
            content: "[2026-09-25 18:00:00] [1/6] Fetching source code...\n[2026-09-25 18:00:03] [5/6] Activating release...\n[2026-09-25 18:00:10] Deployed\n",
            truncated: false,
            missing_reason: null,
          }),
      },
      { status: "static", app_type: "static", port: null },
    );
    await waitFor(() => {
      expect(phaseStates()).toMatchObject({ activate: "done", health: "not_applicable" });
    });
    const phases = screen.getByRole("list", { name: "Deploy phases" });
    const health = phases.querySelector<HTMLElement>('[data-phase="health"]');
    if (health === null) throw new Error("no health phase");
    expect(within(health).getByText("Not applicable")).toBeInTheDocument();
    expect(within(health).queryByText("Not in the log")).not.toBeInTheDocument();
    // Measured from the log alone: seven seconds, never an hour of time-zone offset.
    expect(within(phases).getByText("7.0s")).toBeInTheDocument();
    expect(screen.getByText(/Health does not apply: a static site is served as files/)).toBeInTheDocument();
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

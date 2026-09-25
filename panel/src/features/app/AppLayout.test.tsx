import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { FakeEventSource, SESSION, fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const DOMAIN = "shop.example.com";

const JOB = {
  id: "96bad296",
  type: "update",
  name: `Update ${DOMAIN}`,
  description: `Updating the application at ${DOMAIN}`,
  status: "running",
  progress: 0,
  total_steps: 100,
  current_step: "",
  created_at: "2026-09-25T19:21:13",
  logs: [{ timestamp: "2026-09-25T19:21:13", level: "info", message: "Job started", step: 0 }],
  metadata: { domain: DOMAIN },
};

const CERT = {
  domain: DOMAIN,
  domains: [DOMAIN, `www.${DOMAIN}`],
  days_remaining: 29,
  expires_on: "2026-10-24",
  valid_until: "2026-10-24 19:20:49+00:00 (VALID: 28 days)",
  auto_renew: true,
};

async function appAt(extra: Record<string, RouteHandler> = {}, path = `/apps/${DOMAIN}`) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/certs": () => json(200, { certificates: [CERT], total: 1 }),
    "GET /api/sites": () =>
      json(200, { sites: [{ name: DOMAIN, webserver: "nginx", enabled: true, config_path: "/etc/nginx", has_ssl: true }], total: 1, webserver: "nginx" }),
    "GET /api/deployments": () =>
      json(200, {
        items: [
          { id: 12, domain: DOMAIN, status: "failed", triggered_by: "panel", git_commit: "c07d5e3", git_branch: "main", has_log: true, started_at: "2026-09-25T19:20:35", finished_at: "2026-09-25T19:21:00", error: "npm ERR!" },
          { id: 11, domain: DOMAIN, status: "success", triggered_by: "cli", git_commit: "9f2c41a", git_branch: "main", has_log: true, started_at: "2026-09-25T18:20:35", finished_at: "2026-09-25T18:21:00" },
        ],
        total: 2,
        next_before_id: null,
      }),
    [`GET /api/apps/${DOMAIN}/webhook/deliveries`]: () => json(200, { items: [], total: 0 }),
    "GET /api/jobs/active": () => json(200, { jobs: [], total: 0, active: 0 }),
    "POST /api/jobs/update": () => json(202, { message: "Update job created", job: JOB }),
    [`GET /api/jobs/${JOB.id}`]: () => json(200, JOB),
    ...extra,
  });
  const harness = renderConsole(path);
  await screen.findByRole("heading", { level: 1 });
  return { ...harness, backend };
}

function header() {
  const h1 = screen.getByRole("heading", { level: 1, name: DOMAIN });
  const found = h1.closest("header");
  if (!found) throw new Error("no page header");
  return found;
}

describe("an application's page", () => {
  it("heads the page with its state, type, port and a link to the live site", async () => {
    await appAt();
    const top = header();
    expect(await within(top).findByText("Running")).toBeInTheDocument();
    expect(within(top).getByText("nextjs")).toBeInTheDocument();
    expect(within(top).getByText("3000")).toBeInTheDocument();
    const live = within(top).getByRole("link", { name: /shop\.example\.com/ });
    await waitFor(() => {
      expect(live).toHaveAttribute("href", `https://${DOMAIN}`);
    });
    expect(live).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("navigation", { name: "Application sections" })).toBeInTheDocument();
  });

  it("queues an update, shows it running, and keeps its failure on screen from the job events", async () => {
    const { user, backend } = await appAt();
    const update = await within(header()).findByRole("button", { name: "Update" });
    await user.click(update);
    await waitFor(() => {
      expect(backend.callsTo("POST /api/jobs/update")[0]?.body).toEqual({ domain: DOMAIN });
    });
    expect(await within(header()).findByText("Updating")).toHaveAttribute("data-state", "deploying");
    expect(screen.getByText(`Updating ${DOMAIN}`)).toBeInTheDocument();

    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().emit("job", {
        ...JOB,
        status: "failed",
        error: "Application not found: shop.example.com",
        logs: [{ message: "Job failed", level: "error" }],
      });
    });
    expect(await screen.findByText(`Update of ${DOMAIN} failed`)).toBeInTheDocument();
    expect(screen.getByText("Application not found: shop.example.com")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(`Update of ${DOMAIN} failed`)).not.toBeInTheDocument();
  });

  it("follows the app event: a deploy elsewhere turns the header to deploying", async () => {
    const { queryClient } = await appAt();
    await within(header()).findByText("Running");
    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().emit("app", { domain: DOMAIN, status: "deploying" });
    });
    expect(await within(header()).findByText("Deploying")).toBeInTheDocument();
    expect(queryClient.getQueryData(["app", DOMAIN])).toMatchObject({ status: "deploying" });
  });

  it("deletes only once the domain is typed, then returns to the list", async () => {
    const { user, backend, location } = await appAt({
      // Already confirmed it's them: the typed confirmation opens straight away.
      "GET /api/auth/session": () => json(200, { ...SESSION, elevated_until: "2999-01-01T00:00:00+00:00" }),
      [`DELETE /api/apps/${DOMAIN}`]: () =>
        json(202, { job_id: JOB.id, status: "pending", message: `Deletion queued for ${DOMAIN}`, job: { ...JOB, type: "delete" } }),
    });
    await user.click(await within(header()).findByRole("button", { name: "More actions" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete application" }));
    const dialog = await screen.findByRole("alertdialog");
    const confirm = within(dialog).getByRole("button", { name: "Delete application" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), DOMAIN);
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(location().pathname).toBe("/apps");
    });
    const call = backend.callsTo(`DELETE /api/apps/${DOMAIN}`)[0];
    expect(Object.fromEntries(call?.search ?? [])).toEqual({ remove_files: "true", remove_ssl: "true" });
    expect(backend.callsTo("POST /api/jobs/delete")).toHaveLength(0);
  });

  it("asks before stopping", async () => {
    const { user, backend } = await appAt({
      [`POST /api/apps/${DOMAIN}/stop`]: () => json(200, { success: true, message: "Application stopped", domain: DOMAIN }),
    });
    await user.click(await within(header()).findByRole("button", { name: "More actions" }));
    await user.click(await screen.findByRole("menuitem", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog", { name: `Stop ${DOMAIN}?` });
    expect(backend.callsTo(`POST /api/apps/${DOMAIN}/stop`)).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Stop application" }));
    await waitFor(() => {
      expect(backend.callsTo(`POST /api/apps/${DOMAIN}/stop`)).toHaveLength(1);
    });
  });

  it("rolls an in-place app back to a chosen backup", async () => {
    const { user, backend } = await appAt({
      [`GET /api/apps/${DOMAIN}/rollback-points`]: () =>
        json(200, {
          items: [{ id: "shop-example-com_20260923_182035", created_at: "2026-09-23T18:20:35", description: "Nightly backup", size_bytes: 2048, git_commit: "9f2c41a" }],
          total: 1,
        }),
      "POST /api/jobs/rollback": () => json(202, { message: "Rollback job created", job: { ...JOB, type: "restore" } }),
    });
    await user.click(await within(header()).findByRole("button", { name: "More actions" }));
    await user.click(await screen.findByRole("menuitem", { name: "Roll back" }));
    const dialog = await screen.findByRole("dialog", { name: `Roll back ${DOMAIN}` });
    await user.click(await within(dialog).findByRole("radio", { name: /shop-example-com_20260923_182035/ }));
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/jobs/rollback")[0]?.body).toEqual({ domain: DOMAIN, backup_id: "shop-example-com_20260923_182035" });
    });
  });

  it("says plainly when there is no such application", async () => {
    await appAt({ "GET /api/apps/gone.example.com": () => problem(404, "not_found", "Application not found: gone.example.com") }, "/apps/gone.example.com");
    expect(await screen.findByRole("heading", { level: 2, name: "No application at this domain" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Application sections" })).not.toBeInTheDocument();
  });

  describe("its overview", () => {
    it("summarises the current commit, the last deploys as dots, uptime and certificate", async () => {
      await appAt();
      expect(await screen.findByText("9f2c41a")).toBeInTheDocument();
      const dots = await screen.findByRole("list", { name: "Last 2 deploys, oldest first" });
      const links = within(dots).getAllByRole("link");
      expect(links.map((link) => link.getAttribute("href"))).toEqual([
        `/apps/${DOMAIN}/deployments/11`,
        `/apps/${DOMAIN}/deployments/12`,
      ]);
      expect(links[1]).toHaveAccessibleName(/^Deploy 12: Failed c07d5e3/);
      expect(await screen.findByText("29 days left")).toBeInTheDocument();
    });

    it("lists the domains the certificate covers and the runtime facts", async () => {
      await appAt();
      const domains = await screen.findByRole("region", { name: "Domains" });
      expect(await within(domains).findByRole("link", { name: /^www\.shop\.example\.com/ })).toBeInTheDocument();
      expect(within(domains).getAllByText("Certificate valid for 29 days")).toHaveLength(2);
      const runtime = screen.getByRole("region", { name: "Runtime" });
      expect(within(runtime).getByText("Port")).toBeInTheDocument();
      expect(within(runtime).getByText("In place")).toBeInTheDocument();
    });

    it("has no accessibility violations", async () => {
      await appAt();
      await screen.findByText("29 days left");
      await expectNoAxeViolations(screen.getByRole("main"));
    });
  });
});

describe("a job that ends before its queueing answer arrives", () => {
  it("keeps the newer state the stream already delivered", async () => {
    let answer: (response: Response) => void = () => undefined;
    const { user } = await appAt({
      "POST /api/jobs/update": () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
      // The job's own read never answers: only the cache can say how it ended.
      [`GET /api/jobs/${JOB.id}`]: () => new Promise<Response>(() => undefined),
    });
    await user.click(await within(header()).findByRole("button", { name: "Update" }));
    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().emit("job", { ...JOB, status: "failed", error: "boom" });
    });
    await act(async () => {
      answer(json(202, { message: "Update job created", job: JOB }));
      await Promise.resolve();
    });
    expect(await screen.findByText(`Update of ${DOMAIN} failed`)).toBeInTheDocument();
    expect(within(header()).queryByText("Updating")).not.toBeInTheDocument();
  });
});

describe("an app whose last deploy failed", () => {
  it("says so above the tiles, verbatim, with the log and the diagnosis one step away", async () => {
    await appAt();
    const line = await screen.findByText("npm ERR!");
    expect(line.tagName).toBe("CODE");
    expect(screen.getByText(/^The last deploy failed/)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "View log" })[0]).toHaveAttribute("href", `/apps/${DOMAIN}/deployments/12`);
    expect(screen.getAllByRole("link", { name: "Diagnose" }).some((link) => link.getAttribute("href") === `/apps/${DOMAIN}/diagnose`)).toBe(true);
  });
});

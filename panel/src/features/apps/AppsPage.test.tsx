import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const JOB = {
  id: "96bad296",
  type: "update",
  name: "Update shop.example.com",
  description: "Updating",
  status: "running",
  progress: 0,
  total_steps: 100,
  current_step: "",
  created_at: "2026-09-25T19:21:13",
  logs: [],
  metadata: { domain: "shop.example.com" },
};

async function appsAt(path = "/apps", extra: Record<string, RouteHandler> = {}) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/deployments": () =>
      json(200, {
        items: [{ id: 4, domain: "shop.example.com", status: "success", triggered_by: "cli", has_log: true, started_at: "2026-09-25T10:00:00", finished_at: "2026-09-25T10:00:30" }],
        total: 1,
        next_before_id: null,
      }),
    "POST /api/jobs/update": () => json(202, { message: "Update job created", job: JOB }),
    ...extra,
  });
  const harness = renderConsole(path);
  await screen.findByRole("heading", { level: 1, name: "Applications" });
  const table = await screen.findByRole("region", { name: /Applications/ });
  return { ...harness, backend, table };
}

describe("the applications list", () => {
  it("lists every app with its state, type, port and last deploy", async () => {
    const { table } = await appsAt();
    const shop = await within(table).findByRole("link", { name: "shop.example.com" });
    expect(shop).toHaveAttribute("href", "/apps/shop.example.com");
    const row = shop.closest("tr");
    if (!row) throw new Error("no row");
    expect(within(row).getByText("Running")).toBeInTheDocument();
    expect(within(row).getByText("nextjs")).toBeInTheDocument();
    expect(within(row).getByText("3000")).toBeInTheDocument();
    expect(within(row).getByText(/Succeeded/)).toHaveClass("sr-only");
    expect(screen.getByText("2 applications")).toHaveAttribute("role", "status");
  });

  it("filters by the URL's search params", async () => {
    const { table } = await appsAt("/apps?state=failed");
    await within(table).findByRole("link", { name: "admin.example.com" });
    expect(within(table).queryByRole("link", { name: "shop.example.com" })).not.toBeInTheDocument();
    expect(screen.getByText("1 of 2 applications")).toBeInTheDocument();
  });

  it("writes the search into the URL as it is typed, and / focuses it", async () => {
    const { user, location, table } = await appsAt();
    await within(table).findByRole("link", { name: "shop.example.com" });
    await user.keyboard("/");
    const search = screen.getByRole("searchbox", { name: "Search applications" });
    expect(search).toHaveFocus();
    await user.type(search, "admin");
    await waitFor(() => {
      expect(location().search).toEqual({ q: "admin" });
    });
    expect(within(table).queryByRole("link", { name: "shop.example.com" })).not.toBeInTheDocument();
  });

  it("says when nothing matches and clears the filters", async () => {
    const { user, location } = await appsAt("/apps?q=nothing-like-this");
    expect(await screen.findByText("No application matches")).toBeInTheDocument();
    const clear = screen.getAllByRole("button", { name: "Clear filters" });
    await user.click(clear[clear.length - 1] ?? clear[0] ?? document.body);
    await waitFor(() => {
      expect(location().search).toEqual({});
    });
  });

  it("queues an update from a row's menu through the typed client", async () => {
    const { user, backend, table } = await appsAt();
    await within(table).findByRole("link", { name: "shop.example.com" });
    await user.click(screen.getByRole("button", { name: "Actions for shop.example.com" }));
    await user.click(await screen.findByRole("menuitem", { name: "Update" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/jobs/update")).toHaveLength(1);
    });
    expect(backend.callsTo("POST /api/jobs/update")[0]?.body).toEqual({ domain: "shop.example.com", force: false });
    expect(await screen.findByText("Update of shop.example.com queued")).toBeInTheDocument();
  });

  it("asks a row's update whether to rebuild the same commit when nothing is new", async () => {
    const { user, backend, table } = await appsAt("/apps", {
      "POST /api/jobs/update": (call) =>
        (call.body as { force?: boolean }).force === true
          ? json(202, {
              message: "Update job created",
              job: { id: "0badcafe", type: "update", name: "", description: "", status: "pending", progress: 0, total_steps: 100, current_step: "", created_at: "2026-09-25T19:00:00", logs: [], metadata: { domain: "shop.example.com" } },
            })
          : problem(409, "nothing_new", "No new commits on main since 9f2c41a, which is live", { hint: "Update with force to rebuild it anyway." }),
    });
    await within(table).findByRole("link", { name: "shop.example.com" });
    await user.click(screen.getByRole("button", { name: "Actions for shop.example.com" }));
    await user.click(await screen.findByRole("menuitem", { name: "Update" }));
    const dialog = await screen.findByRole("dialog", { name: "Nothing new to deploy to shop.example.com" });
    expect(within(dialog).getByText("No new commits on main since 9f2c41a, which is live")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Rebuild anyway" }));
    expect(await screen.findByText("Update of shop.example.com queued")).toBeInTheDocument();
    expect(backend.callsTo("POST /api/jobs/update")[1]?.body).toEqual({ domain: "shop.example.com", force: true });
  });

  it("restarts from a row's menu, and not a static site that has nothing to restart", async () => {
    const { user, backend, table } = await appsAt("/apps", {
      "GET /api/apps": () =>
        json(200, {
          total: 2,
          apps: [
            { domain: "shop.example.com", name: "shop", app_type: "nextjs", status: "running", active: true, enabled: true, layout: "inplace" },
            { domain: "landing.example.com", name: "landing", app_type: "static", status: "static", active: false, enabled: false, layout: "inplace" },
          ],
        }),
      "POST /api/apps/shop.example.com/restart": () => json(200, { success: true, message: "Application restarted", domain: "shop.example.com" }),
    });
    await within(table).findByRole("link", { name: "landing.example.com" });
    await user.click(screen.getByRole("button", { name: "Actions for landing.example.com" }));
    expect(await screen.findByRole("menuitem", { name: "Restart" })).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("button", { name: "Actions for shop.example.com" }));
    await user.click(await screen.findByRole("menuitem", { name: "Restart" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/apps/shop.example.com/restart")).toHaveLength(1);
    });
    expect(await screen.findByText("Restarted shop.example.com")).toBeInTheDocument();
  });

  it("invites the first deploy on an empty machine", async () => {
    fakeBackend({ ...signedInRoutes(), "GET /api/apps": () => json(200, { total: 0, apps: [] }) });
    renderConsole("/apps");
    expect(await screen.findByRole("heading", { level: 2, name: "Deploy your first application" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "New application" }).length).toBeGreaterThan(0);
  });

  it("has no accessibility violations", async () => {
    const { table } = await appsAt();
    await within(table).findByRole("link", { name: "shop.example.com" });
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

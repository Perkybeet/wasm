import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../../test/axe";
import { renderConsole } from "../../../test/console";
import { fakeBackend, json, problem } from "../../../test/fakes";
import type { RouteHandler } from "../../../test/fakes";
import { TAB_DOMAIN, appRoutes } from "../testRoutes";

function deploy(id: number, status = "success", extra: Record<string, unknown> = {}) {
  return {
    id,
    domain: TAB_DOMAIN,
    status,
    triggered_by: id % 2 ? "webhook" : "cli",
    git_commit: `c0ffe${String(id).padStart(2, "0")}`,
    git_branch: "main",
    started_at: "2026-09-25T18:00:00",
    finished_at: "2026-09-25T18:00:47",
    duration_s: 47,
    error: null,
    has_log: true,
    ...extra,
  };
}

/** Thirteen deploys, newest first, served ten at a time with keyset pagination. */
const history: RouteHandler = (call) => {
  const all = Array.from({ length: 13 }, (_, i) =>
    deploy(25 - i, i === 5 ? "failed" : "success", i === 0 ? { commit_message: "Redesign checkout summary" } : {}),
  );
  const before = call.search.get("before_id");
  const limit = Number(call.search.get("limit") ?? "50");
  const rows = before === null ? all : all.filter((row) => row.id < Number(before));
  const page = rows.slice(0, limit);
  return json(200, { items: page, total: all.length, next_before_id: rows.length > limit ? (page.at(-1)?.id ?? null) : null });
};

const RELEASES = {
  domain: TAB_DOMAIN,
  items: [
    { id: "20260925-184247-2a8b7c4", commit: "2a8b7c4", created_at: "2026-09-25T18:42:47+00:00", activated_at: "2026-09-25T18:43:30+00:00", status: "active", active: true, on_disk: true },
    { id: "20260924-194023-19d3f6e", commit: "19d3f6e", created_at: "2026-09-24T19:40:23+00:00", activated_at: null, status: "superseded", active: false, on_disk: true },
    { id: "20260916-182823-d08e4f7", commit: "d08e4f7", created_at: "2026-09-16T18:28:23+00:00", activated_at: null, status: "failed", active: false, on_disk: false },
  ],
  total: 3,
};

async function tabAt(app: Record<string, unknown>, extra: Record<string, RouteHandler> = {}) {
  const backend = fakeBackend(appRoutes(app, { "GET /api/deployments": history, ...extra }));
  const harness = renderConsole(`/apps/${TAB_DOMAIN}/deployments`);
  await screen.findByRole("heading", { level: 2, name: "History" });
  return { ...harness, backend };
}

// Whole-console renders: generous under a loaded machine or a slow CI runner.
describe("the deployments tab", { timeout: 20_000 }, () => {
  it("lists the deploys newest first, each linking to its page, and loads older ones by keyset", async () => {
    const { user, backend } = await tabAt({});
    const table = await screen.findByRole("region", { name: `Deploys of ${TAB_DOMAIN}, newest first` });
    await within(table).findByRole("link", { name: "Deployment 25" });
    expect(within(table).getAllByRole("row")).toHaveLength(11);
    expect(within(table).getByRole("link", { name: "Deployment 25" })).toHaveAttribute("href", `/apps/${TAB_DOMAIN}/deployments/25`);
    expect(within(table).getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Showing 10 of 13")).toBeInTheDocument();
    // The commit's own subject line, beside its hash, truncated but reachable in full on hover.
    expect(within(table).getByTitle("Redesign checkout summary")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load older deploys" }));
    await within(table).findByRole("link", { name: "Deployment 13" });
    expect(within(table).getAllByRole("row")).toHaveLength(14);
    const pages = backend.callsTo("GET /api/deployments").filter((call) => call.search.get("domain") === TAB_DOMAIN);
    expect(pages.at(-1)?.search.get("before_id")).toBe("16");
    expect(screen.queryByRole("button", { name: "Load older deploys" })).not.toBeInTheDocument();
  });

  it("rolls a release app back to an earlier release in one step, and says it went back", async () => {
    const { user, backend } = await tabAt(
      { layout: "releases" },
      {
        [`GET /api/apps/${TAB_DOMAIN}/releases`]: () => json(200, RELEASES),
        [`POST /api/apps/${TAB_DOMAIN}/releases/20260924-194023-19d3f6e/activate`]: () =>
          json(200, {
            domain: TAB_DOMAIN,
            release_id: "20260924-194023-19d3f6e",
            previous_id: "20260925-184247-2a8b7c4",
            changed: true,
            rolled_back: true,
            deployment_id: 26,
          }),
      },
    );
    const releases = await screen.findByRole("list", { name: `Releases of ${TAB_DOMAIN}, newest first` });
    expect(within(releases).getByText("Serving")).toBeInTheDocument();
    expect(within(releases).getByText("Removed from disk")).toBeInTheDocument();
    // Only a release on disk that is not serving can be switched to.
    expect(within(releases).getAllByRole("button")).toHaveLength(1);

    await user.click(within(releases).getByRole("button", { name: /Roll back to this/ }));
    const dialog = await screen.findByRole("dialog", { name: "Roll back to this release?" });
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => {
      expect(backend.callsTo(`POST /api/apps/${TAB_DOMAIN}/releases/20260924-194023-19d3f6e/activate`)).toHaveLength(1);
    });
    expect(await screen.findAllByText(`Rolled ${TAB_DOMAIN} back to release 20260924-194023-19d3f6e`, {}, { timeout: 5_000 })).not.toHaveLength(0);
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Roll back to this release?" })).not.toBeInTheDocument();
    });
  });

  it("keeps the dialog open with the health check's own words when the release is refused", async () => {
    const { user } = await tabAt(
      { layout: "releases" },
      {
        [`GET /api/apps/${TAB_DOMAIN}/releases`]: () => json(200, RELEASES),
        [`POST /api/apps/${TAB_DOMAIN}/releases/20260924-194023-19d3f6e/activate`]: () =>
          problem(500, "deploymenterror", "Release 20260924-194023-19d3f6e did not pass its health check", {
            hint: "The release that was serving is active again.",
          }),
      },
    );
    const releases = await screen.findByRole("list", { name: `Releases of ${TAB_DOMAIN}, newest first` });
    await user.click(within(releases).getByRole("button", { name: /Roll back to this/ }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));
    expect(await within(dialog).findByText("Release 20260924-194023-19d3f6e did not pass its health check", {}, { timeout: 5_000 })).toBeInTheDocument();
    expect(within(dialog).getByText("The release that was serving is active again.")).toBeInTheDocument();
  });

  it("offers an in-place app its backups, and queues the rollback as a job", async () => {
    const { user, backend } = await tabAt(
      {},
      {
        [`GET /api/apps/${TAB_DOMAIN}/rollback-points`]: () =>
          json(200, {
            items: [{ id: "shop-example-com_20260923_182035", created_at: "2026-09-23T18:20:35", description: "Nightly backup", size_bytes: 2048, git_commit: "9f2c41a" }],
            total: 1,
          }),
        "POST /api/jobs/rollback": () =>
          json(202, {
            message: "Rollback job created",
            job: { id: "0a1b2c3d", type: "restore", name: "Rollback", description: "", status: "pending", progress: 0, total_steps: 100, current_step: "", created_at: "2026-09-25T19:00:00", logs: [], metadata: { domain: TAB_DOMAIN } },
          }),
      },
    );
    const points = await screen.findByRole("list", { name: `Backups of ${TAB_DOMAIN}, newest first` });
    await user.click(within(points).getByRole("button", { name: /Roll back to this/ }));
    const dialog = await screen.findByRole("dialog", { name: "Roll back to this backup?" });
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/jobs/rollback")[0]?.body).toEqual({ domain: TAB_DOMAIN, backup_id: "shop-example-com_20260923_182035" });
    });
  });

  it("says what the history is for when there is none yet", async () => {
    await tabAt({}, { "GET /api/deployments": () => json(200, { items: [], total: 0, next_before_id: null }) });
    expect(await screen.findByRole("heading", { name: "No deploys recorded yet" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    await tabAt({ layout: "releases" }, { [`GET /api/apps/${TAB_DOMAIN}/releases`]: () => json(200, RELEASES) });
    await screen.findByRole("list", { name: `Releases of ${TAB_DOMAIN}, newest first` });
    await screen.findByRole("link", { name: "Deployment 25" });
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

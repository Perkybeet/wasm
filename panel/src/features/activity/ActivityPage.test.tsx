import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RecordedCall, RouteHandler } from "../../test/fakes";

const JOBS = [
  {
    id: "a1",
    type: "update",
    name: "Update shop.example.com",
    description: "Updating the application at shop.example.com",
    status: "completed",
    progress: 100,
    total_steps: 100,
    current_step: "",
    created_at: "2026-09-25T09:00:00Z",
    started_at: "2026-09-25T09:00:01Z",
    completed_at: "2026-09-25T09:00:42Z",
    result: null,
    error: null,
    logs: [],
    metadata: { domain: "shop.example.com" },
    actor: "master",
  },
  {
    id: "b2",
    type: "deploy",
    name: "Deploy admin.example.com",
    description: "Deploying admin.example.com",
    status: "failed",
    progress: 40,
    total_steps: 100,
    current_step: "",
    created_at: "2026-09-25T08:00:00Z",
    started_at: "2026-09-25T08:00:01Z",
    completed_at: "2026-09-25T08:00:12Z",
    result: null,
    error: "Build failed: exit code 1",
    logs: [],
    metadata: { domain: "admin.example.com" },
    actor: "token:ci-deploy",
  },
];

const AUDIT_ENTRIES = [
  {
    timestamp: "2026-09-25T09:30:00+00:00",
    action: "auth.login",
    result: "success",
    actor: "9f8e7d6c5b4a",
    client_ip: "203.0.113.1",
    resource: "/api/auth/login",
    detail: null,
  },
  {
    timestamp: "2026-09-25T07:00:00+00:00",
    action: "auth.scope",
    result: "denied",
    actor: "token:ci-deploy",
    client_ip: "203.0.113.7",
    resource: "/api/apps/blog.example.com",
    detail: "scope 'deploy' below required 'admin'",
  },
];

function jobsHandler(all: typeof JOBS) {
  return ({ search }: RecordedCall) => {
    const status = search.get("status");
    const matched = status ? all.filter((job) => job.status === status) : all;
    return json(200, { jobs: matched, total: matched.length, active: 0 });
  };
}

function auditHandler(all: typeof AUDIT_ENTRIES) {
  return ({ search }: RecordedCall) => {
    const result = search.get("result");
    const matched = result ? all.filter((entry) => entry.result === result) : all;
    return json(200, { items: matched, next_before: null });
  };
}

async function activityAt(routes: Record<string, RouteHandler> = {}) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/jobs": jobsHandler(JOBS),
    "GET /api/audit": auditHandler(AUDIT_ENTRIES),
    ...routes,
  });
  const harness = renderConsole("/activity");
  await screen.findByRole("heading", { level: 1, name: "Activity" });
  const table = await screen.findByRole("region", { name: /Activity/ });
  return { ...harness, backend, table };
}

describe("the activity timeline", () => {
  it("merges the jobs history and the audit log, newest first", async () => {
    const { table } = await activityAt();
    await within(table).findByText("shop.example.com");
    expect(within(table).getByText("admin.example.com")).toBeInTheDocument();
    expect(within(table).getByText("Sign-in attempt")).toBeInTheDocument();
    expect(within(table).getByText("Failed a scope check")).toBeInTheDocument();
  });

  it("shows a quiet note instead of an error when the session cannot read the audit log", async () => {
    const { table } = await activityAt({ "GET /api/audit": () => problem(403, "forbidden", "admin scope required") });
    await within(table).findByText("shop.example.com");
    expect(screen.getByText(/audit log needs an admin token/)).toBeInTheDocument();
    expect(screen.queryByText(/^Could not load/)).not.toBeInTheDocument();
    expect(within(table).queryByText("Sign-in attempt")).not.toBeInTheDocument();
  });

  it("filters by kind through the URL, excluding the other source entirely", async () => {
    const { table, location, user } = await activityAt();
    await within(table).findByText("shop.example.com");
    await user.click(screen.getByRole("combobox", { name: "Kind" }));
    await user.click(await screen.findByRole("option", { name: "Audited actions" }));
    await waitFor(() => {
      expect(location().search).toEqual({ kind: "audit" });
    });
    await waitFor(() => {
      expect(within(table).queryByText("shop.example.com")).not.toBeInTheDocument();
    });
    expect(within(table).getByText("Sign-in attempt")).toBeInTheDocument();
  });

  it("filters by actor through the URL", async () => {
    const { table, location, user } = await activityAt();
    await within(table).findByText("shop.example.com");
    await user.type(screen.getByRole("searchbox", { name: "Actor" }), "master");
    await waitFor(() => {
      expect(location().search).toEqual({ actor: "master" });
    });
    await waitFor(() => {
      expect(within(table).queryByText("admin.example.com")).not.toBeInTheDocument();
    });
    expect(within(table).getByText("shop.example.com")).toBeInTheDocument();
  });

  it("invites nothing to create on an empty machine: activity has no wizard", async () => {
    fakeBackend({
      ...signedInRoutes(),
      "GET /api/jobs": () => json(200, { jobs: [], total: 0, active: 0 }),
      "GET /api/audit": () => json(200, { items: [], next_before: null }),
    });
    renderConsole("/activity");
    expect(await screen.findByRole("heading", { level: 2, name: "Nothing has run yet" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { table } = await activityAt();
    await within(table).findByText("shop.example.com");
    await expectNoAxeViolations(screen.getByRole("main"));
  });

  it("has no accessibility violations on the admin-required note", async () => {
    const { table } = await activityAt({ "GET /api/audit": () => problem(403, "forbidden", "admin scope required") });
    await within(table).findByText("shop.example.com");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

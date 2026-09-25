import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import type { RecordedCall } from "../../test/fakes";

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
  },
];

function jobsHandler(all: typeof JOBS) {
  return ({ search }: RecordedCall) => {
    const status = search.get("status");
    const matched = status ? all.filter((job) => job.status === status) : all;
    return json(200, { jobs: matched, total: matched.length, active: 0 });
  };
}

async function activityAt() {
  const backend = fakeBackend({ ...signedInRoutes(), "GET /api/jobs": jobsHandler(JOBS) });
  const harness = renderConsole("/activity");
  await screen.findByRole("heading", { level: 1, name: "Activity" });
  const table = await screen.findByRole("region", { name: /Activity/ });
  return { ...harness, backend, table };
}

describe("the activity timeline", () => {
  it("lists the seeded jobs and says it is not a full audit log", async () => {
    const { table } = await activityAt();
    await within(table).findByText("shop.example.com");
    expect(within(table).getByText("admin.example.com")).toBeInTheDocument();
    expect(screen.getByText(/not a full audit log/)).toBeInTheDocument();
  });

  it("filters by result through the URL, re-querying the server", async () => {
    const { table, location } = await activityAt();
    await within(table).findByText("shop.example.com");
    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByRole("combobox", { name: "Result" }));
    await user.click(await screen.findByRole("option", { name: "failed" }));
    await waitFor(() => {
      expect(location().search).toEqual({ status: "failed" });
    });
    await waitFor(() => {
      expect(within(table).queryByText("shop.example.com")).not.toBeInTheDocument();
    });
    expect(within(table).getByText("admin.example.com")).toBeInTheDocument();
  });

  it("invites nothing to create on an empty machine: activity has no wizard", async () => {
    fakeBackend({ ...signedInRoutes(), "GET /api/jobs": () => json(200, { jobs: [], total: 0, active: 0 }) });
    renderConsole("/activity");
    expect(await screen.findByRole("heading", { level: 2, name: "Nothing has run yet" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { table } = await activityAt();
    await within(table).findByText("shop.example.com");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";
import type { Inspection } from "./wizard";

const INSPECTION: Inspection = {
  app_type: "nextjs",
  detected_types: ["nextjs", "nodejs"],
  package_manager: "npm",
  install_command: ["npm", "ci"],
  build_command: ["npm", "run", "build"],
  start_command: "npm run start",
  default_port: 3000,
  env_keys: [
    { name: "DATABASE_URL", default: null, secret: false, required: true },
    { name: "NEXTAUTH_SECRET", default: null, secret: true, required: true },
    { name: "LOG_LEVEL", default: "info", secret: false, required: false },
  ],
  branch: "",
  commit: "",
};

const JOB = {
  id: "0dd5e7a1",
  type: "deploy",
  name: "Deploy storefront.example.com",
  description: "Deploying",
  status: "running",
  progress: 10,
  total_steps: 100,
  current_step: "Deploying",
  created_at: "2026-09-25T19:21:13",
  logs: [],
  metadata: { domain: "storefront.example.com" },
};

function wizard(extra: Record<string, RouteHandler> = {}) {
  let deployed = false;
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/config/webserver": () => json(200, { webserver: "nginx" }),
    "POST /api/apps/inspect": () => json(200, INSPECTION),
    "GET /api/deployments": () =>
      json(200, {
        items: deployed
          ? [{ id: 7, domain: "storefront.example.com", status: "running", triggered_by: "panel", has_log: true }]
          : [{ id: 3, domain: "storefront.example.com", status: "success", triggered_by: "cli", has_log: false }],
        total: 1,
        next_before_id: null,
      }),
    "POST /api/apps": () => {
      deployed = true;
      return json(202, { job_id: JOB.id, status: "pending", message: "Deployment queued", job: JOB });
    },
    [`GET /api/jobs/${JOB.id}`]: () => json(200, JOB),
    "GET /api/jobs/active": () => json(200, { jobs: [JOB], total: 1, active: 1 }),
    ...extra,
  });
  return { backend, harness: renderConsole("/apps/new") };
}

async function inspect(user: ReturnType<typeof renderConsole>["user"]) {
  await screen.findByRole("heading", { level: 1, name: "New application" });
  await user.type(screen.getByLabelText("Repository or directory"), "/var/www/src/storefront");
  await user.click(screen.getByRole("button", { name: "Inspect source" }));
  return screen.findByRole("heading", { level: 2, name: "Review" });
}

describe("the new-app wizard", () => {
  it("inspects the source, proposes what it found, and hands over to the deployment once it starts", { timeout: 20_000 }, async () => {
    const { backend, harness } = wizard();
    const { user } = harness;
    const heading = await inspect(user);
    expect(heading).toHaveFocus();
    expect(backend.callsTo("POST /api/apps/inspect")[0]?.body).toEqual({ source: "/var/www/src/storefront" });

    // What was found, shown as it will run; the type is a choice with the detected one on top.
    expect(screen.getByText("npm ci")).toBeInTheDocument();
    expect(screen.getByText("npm run build")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Deploy as" })).toHaveTextContent("Next.js");
    // The source is not asked again.
    expect(screen.queryByLabelText("Repository or directory")).not.toBeInTheDocument();
    // shop.example.com holds 3000 in the fake machine, so the next free port is proposed.
    expect(screen.getByLabelText("Port")).toHaveValue("3001");

    await user.type(screen.getByLabelText("Domain"), "storefront.example.com");
    await user.type(screen.getByLabelText(/^DATABASE_URL/), "postgres://db/storefront");
    const secret = screen.getByLabelText(/^NEXTAUTH_SECRET/);
    expect(secret).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Generate NEXTAUTH_SECRET" }));
    expect((secret as HTMLInputElement).value).toMatch(/^[A-Za-z0-9_-]{43}$/);
    await expectNoAxeViolations(document.body);

    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Deploy" })).toHaveFocus();
    expect(screen.getByText("https://storefront.example.com")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Deploy storefront.example.com" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/apps")).toHaveLength(1);
    });
    expect(backend.callsTo("POST /api/apps")[0]?.body).toEqual({
      domain: "storefront.example.com",
      source: "/var/www/src/storefront",
      app_type: "nextjs",
      port: 3001,
      webserver: "nginx",
      ssl: true,
      layout: "releases",
      env_vars: {
        DATABASE_URL: "postgres://db/storefront",
        NEXTAUTH_SECRET: (secret as HTMLInputElement).value,
        LOG_LEVEL: "info",
      },
      skip_database: false,
    });
    // The deploy's own history row is the first one newer than the newest before it (3).
    await waitFor(
      () => {
        expect(harness.location().pathname).toBe("/apps/storefront.example.com/deployments/7");
      },
      { timeout: 4_000 },
    );
  });

  it("keeps the required variables and the domain from being skipped", async () => {
    const { harness } = wizard();
    await inspect(harness.user);
    await harness.user.type(screen.getByLabelText("Domain"), "shop.example.com");
    await harness.user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText(/shop\.example\.com is already deployed/)).toBeInTheDocument();
    expect(screen.getAllByText(".env.example gives it no value, so the app expects one.")).toHaveLength(2);
    expect(screen.getByRole("heading", { level: 2, name: "Review" })).toBeInTheDocument();
  });

  it("sends a domain the server refuses back to its field on the Review step", async () => {
    const { harness } = wizard({
      "POST /api/apps": () => problem(409, "conflict", "Application already exists: storefront.example.com"),
    });
    const { user } = harness;
    await inspect(user);
    await user.type(screen.getByLabelText("Domain"), "storefront.example.com");
    await user.type(screen.getByLabelText(/^DATABASE_URL/), "x");
    await user.type(screen.getByLabelText(/^NEXTAUTH_SECRET/), "y");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Deploy storefront.example.com" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Review" })).toBeInTheDocument();
    expect(screen.getByText("Application already exists: storefront.example.com")).toBeInTheDocument();
  });

  it("shows why an inspection failed, and lets the type be chosen when none matched", async () => {
    const { harness } = wizard({
      "POST /api/apps/inspect": () =>
        problem(500, "deploymenterror", "Could not identify the application type at /srv/odd", {
          hint: "Nothing under the fetched source matches a registered application type.",
        }),
    });
    const { user } = harness;
    await screen.findByRole("heading", { level: 1, name: "New application" });
    await user.type(screen.getByLabelText("Repository or directory"), "/srv/odd");
    await user.click(screen.getByRole("button", { name: "Inspect source" }));
    const detail = await screen.findByText("Could not identify the application type at /srv/odd");
    expect(detail.closest("[role=alert]")).not.toBeNull();
    expect(screen.getByText("Nothing under the fetched source matches a registered application type.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Choose the type yourself" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Review" })).toBeInTheDocument();
    expect(screen.getByText(/No type recognised this source/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Deploy as" })).toHaveTextContent("Choose a type");
  });
});

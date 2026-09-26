import { screen, waitFor, within } from "@testing-library/react";
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

/** Every type `GET /api/apps/types` lists, in the API's own order (alphabetical, auto last). */
const APP_TYPES = {
  types: [
    { type: "docker-compose", name: "Docker Compose", default_port: 3000 },
    { type: "monorepo", name: "Monorepo (Turborepo/pnpm)", default_port: 3000 },
    { type: "nextjs", name: "Next.js", default_port: 3000 },
    { type: "nodejs", name: "Node.js", default_port: 3000 },
    { type: "python", name: "Python (Django/Flask/FastAPI)", default_port: 8000 },
    { type: "static", name: "Static Site", default_port: 80 },
    { type: "vite", name: "Vite (React/Vue/Svelte)", default_port: 5173 },
    { type: "auto", name: "Auto-detect", default_port: 3000 },
  ],
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
  deployment_id: null,
  logs: [],
  metadata: { domain: "storefront.example.com" },
};

/** `GET /api/domains/dns`: everything under `elsewhere.` resolves to another server. */
function dnsRoute(): RouteHandler {
  return (call) => {
    const name = call.search.get("name") ?? "";
    const here = !name.startsWith("elsewhere.");
    return json(200, {
      domain: name,
      expected_addresses: ["203.0.113.10"],
      resolved_addresses: here ? ["203.0.113.10"] : ["198.51.100.23"],
      points_here: here,
    });
  };
}

function wizard(extra: Record<string, RouteHandler> = {}) {
  let deployed = false;
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/config/webserver": () => json(200, { webserver: "nginx" }),
    "GET /api/apps/types": () => json(200, APP_TYPES),
    "GET /api/domains/dns": dnsRoute(),
    "POST /api/apps/inspect": () => json(200, INSPECTION),
    "POST /api/apps": () => {
      deployed = true;
      return json(202, { job_id: JOB.id, status: "pending", message: "Deployment queued", job: JOB });
    },
    // The deployer writes the deployment row (with this job's id) well before the job itself
    // ends, which is what the wizard actually waits for.
    "GET /api/deployments": () =>
      json(200, {
        items: deployed ? [{ id: 7, domain: "storefront.example.com", status: "running", triggered_by: "panel", has_log: true, job_id: JOB.id }] : [],
        total: deployed ? 1 : 0,
        next_before_id: null,
      }),
    [`GET /api/jobs/${JOB.id}`]: () => json(200, JOB),
    "GET /api/jobs/active": () => json(200, { jobs: [JOB], total: 1, active: 1 }),
    ...extra,
  });
  return { backend, harness: renderConsole("/apps/new") };
}

async function inspect(user: ReturnType<typeof renderConsole>["user"], source = "/var/www/src/storefront") {
  await screen.findByRole("heading", { level: 1, name: "New application" });
  await user.type(screen.getByLabelText("Repository or directory"), source);
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

    // What was found, shown as it will run; the type is a choice, its labels from GET /api/apps/types.
    expect(screen.getByText("npm ci")).toBeInTheDocument();
    expect(screen.getByText("npm run build")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Deploy as" })).toHaveTextContent("Next.js");
    // The source is not asked again.
    expect(screen.queryByLabelText("Repository or directory")).not.toBeInTheDocument();
    // shop.example.com holds 3000 in the fake machine, so the next free port is proposed.
    expect(screen.getByLabelText("Port")).toHaveValue("3001");

    await user.type(screen.getByLabelText("Domain"), "storefront.example.com");
    // Where it points, checked as it is typed.
    await screen.findByText("storefront.example.com points here", {}, { timeout: 2_000 });
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
      include_www: false,
      memory_max_mb: null,
      cpu_quota_percent: null,
      tasks_max: null,
      env_vars: {
        DATABASE_URL: "postgres://db/storefront",
        NEXTAUTH_SECRET: (secret as HTMLInputElement).value,
        LOG_LEVEL: "info",
      },
      skip_database: false,
    });
    // The wizard hands over once the deployments list carries a row for this exact job, not
    // once some heuristic guesses which row is this one's.
    await waitFor(() => {
      expect(harness.location().pathname).toBe("/apps/storefront.example.com/deployments/7");
    });
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

  it("shows the fetch failure on the source field, verbatim, with its hint above it", async () => {
    const { harness } = wizard({
      "POST /api/apps/inspect": () =>
        problem(400, "sourceerror", "Download failed: https://example.com/app.tar.gz", { hint: "Name or service not known" }),
    });
    const { user } = harness;
    await screen.findByRole("heading", { level: 1, name: "New application" });
    await user.type(screen.getByLabelText("Repository or directory"), "https://example.com/app.tar.gz");
    await user.click(screen.getByRole("button", { name: "Inspect source" }));
    expect(await screen.findByText("Download failed: https://example.com/app.tar.gz")).toBeInTheDocument();
    expect(screen.getByLabelText("Repository or directory")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Name or service not known")).toBeInTheDocument();
  });

  it("shows git's own output verbatim for a private repository, with the backend's real fix", async () => {
    const { harness } = wizard({
      "POST /api/apps/inspect": () =>
        problem(400, "sourceerror", "Could not access git@github.com:acme/storefront.git", {
          hint: "This looks like a private repository. Add a deploy key, or embed a token in the URL.",
          output: "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
        }),
    });
    const { user, container } = harness;
    await screen.findByRole("heading", { level: 1, name: "New application" });
    await user.type(screen.getByLabelText("Repository or directory"), "git@github.com:acme/storefront.git");
    await user.click(screen.getByRole("button", { name: "Inspect source" }));
    // The short sentence is on the field, where the operator is already looking.
    expect(await screen.findByText("Could not access git@github.com:acme/storefront.git")).toBeInTheDocument();
    // The block below carries git's own report and the backend's actionable fix, verbatim.
    expect(screen.getByText("This looks like a private repository. Add a deploy key, or embed a token in the URL.")).toBeInTheDocument();
    expect(screen.getByText(/Permission denied \(publickey\)/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("offers www and resource limits, and sends them in the deploy request", async () => {
    const { backend, harness } = wizard();
    const { user } = harness;
    await inspect(user);
    await user.type(screen.getByLabelText("Domain"), "newapp.io");
    await user.type(screen.getByLabelText(/^DATABASE_URL/), "x");
    await user.type(screen.getByLabelText(/^NEXTAUTH_SECRET/), "y");

    await user.click(screen.getByRole("checkbox", { name: "Also serve www" }));
    await user.click(screen.getByText("Resource limits"));
    // Scoped: the topbar's machine strip has its own "Memory" and "CPU" meters.
    const limits = within(screen.getByText("Resource limits").closest("details") as HTMLElement);
    await user.type(limits.getByLabelText("Memory"), "512");
    await user.type(limits.getByLabelText("CPU"), "50");

    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Deploy newapp.io" }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/apps")).toHaveLength(1);
    });
    expect(backend.callsTo("POST /api/apps")[0]?.body).toMatchObject({
      domain: "newapp.io",
      include_www: true,
      memory_max_mb: 512,
      cpu_quota_percent: 50,
      tasks_max: null,
    });
  });

  it("warns, without blocking, when the typed domain resolves elsewhere", async () => {
    const { harness } = wizard();
    const { user } = harness;
    await inspect(user);
    await user.type(screen.getByLabelText("Domain"), "elsewhere.example.com");
    await screen.findByText("elsewhere.example.com points somewhere else", {}, { timeout: 2_000 });
    await user.type(screen.getByLabelText(/^DATABASE_URL/), "x");
    await user.type(screen.getByLabelText(/^NEXTAUTH_SECRET/), "y");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Deploy" })).toBeInTheDocument();
  });
});

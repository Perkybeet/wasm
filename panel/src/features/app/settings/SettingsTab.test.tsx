import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../../test/axe";
import { renderConsole } from "../../../test/console";
import { APPS, FakeEventSource, SESSION, fakeBackend, json, problem, signedInRoutes } from "../../../test/fakes";
import type { RouteHandler } from "../../../test/fakes";
import { deletionSummary } from "./DangerSection";
import { planItems } from "./MigrationPlanView";
import { confirmation } from "./ReleasesSection";

const DOMAIN = "shop.example.com";
const BASE = APPS[0];

const RELEASE_APP = {
  ...BASE,
  path: "/var/www/apps/shop-example-com",
  layout: "releases",
  keep_releases: 5,
  source: "https://github.com/shop/storefront.git",
  branch: "main",
  build_command: ["npm", "run", "build"],
  start_command: "npm run start",
  memory_max_mb: 512,
  cpu_quota_percent: 150,
  tasks_max: 256,
};

const IN_PLACE_APP = { ...BASE, path: "/var/www/apps/shop-example-com", layout: "inplace" };

const STATIC_APP = {
  ...BASE,
  app_type: "static",
  status: "static",
  port: null,
  path: "/var/www/apps/landing",
  layout: "inplace",
  source: "https://github.com/you/landing",
  branch: null,
  build_command: [],
  start_command: null,
};

const RELEASES = {
  domain: DOMAIN,
  items: [
    { id: "20260925-184247-2a8b7c4", commit: "2a8b7c4", created_at: "2026-09-25T18:42:47+00:00", activated_at: "2026-09-25T18:43:40+00:00", status: "active", active: true, on_disk: true },
    { id: "20260924-194023-19d3f6e", commit: "19d3f6e", created_at: "2026-09-24T19:40:23+00:00", activated_at: null, status: "superseded", active: false, on_disk: true },
    { id: "20260916-182823-d08e4f7", commit: "d08e4f7", created_at: "2026-09-16T18:28:23+00:00", activated_at: null, status: "failed", active: false, on_disk: false },
  ],
  total: 3,
};

const PLAN = {
  domain: DOMAIN,
  app_path: "/var/www/apps/shop-example-com",
  release_id: "20260925-205239-nogit",
  commit: null,
  persistent: ["uploads", "storage"],
  persistent_source: "common",
  env_files: [".env"],
  unit: "shop-example-com",
  unit_rewrite: true,
  site_rewrite: false,
  untracked_files: [],
  warnings: ["/var/www/apps/shop-example-com is not a git checkout, so what the application wrote for itself cannot be told apart from its code."],
  files: 7,
  bytes: 661,
};

const MIGRATE_JOB = {
  id: "m1",
  type: "migrate",
  name: `Migrate ${DOMAIN}`,
  description: `Moving ${DOMAIN} onto the release layout`,
  status: "pending",
  progress: 0,
  total_steps: 100,
  current_step: "",
  created_at: "2026-09-25T20:53:00",
  logs: [],
  metadata: { domain: DOMAIN },
};

const DELIVERIES = {
  items: [
    { deployment_id: 25, status: "success", started_at: "2026-09-25T19:42:47", git_commit: "2a8b7c4f00", error: null },
    { deployment_id: 21, status: "failed", started_at: "2026-09-16T19:28:23", git_commit: "d08e4f7", error: "Release did not pass its health check\n-- journal --" },
  ],
  total: 2,
};

const ELEVATED = { ...SESSION, elevated_until: "2999-01-01T00:00:00+00:00" };

async function settingsOf(app: object, extra: Record<string, RouteHandler> = {}) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    [`GET /api/apps/${DOMAIN}`]: () => json(200, app),
    "GET /api/certs": () => json(200, { certificates: [], total: 0 }),
    "GET /api/jobs/active": () => json(200, { jobs: [], total: 0, active: 0 }),
    "GET /api/system": () => json(200, { cpu: { cores: 4, percent: 3, load_1min: 0.1, load_5min: 0.1, load_15min: 0.1 } }),
    [`GET /api/apps/${DOMAIN}/releases`]: () => json(200, RELEASES),
    [`GET /api/apps/${DOMAIN}/webhook/deliveries`]: () => json(200, { items: [], total: 0 }),
    ...extra,
  });
  const harness = renderConsole(`/apps/${DOMAIN}/settings`);
  await screen.findByRole("heading", { level: 2, name: "Source and runtime" });
  return { ...harness, backend };
}

function section(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("the Settings tab", () => {
  it("states what the app is, its source, branch and commands, linking an HTTPS repository", async () => {
    await settingsOf(RELEASE_APP);
    const source = section("Source and runtime");
    expect(within(source).getByText("nextjs")).toBeInTheDocument();
    expect(within(source).getByText("/var/www/apps/shop-example-com")).toBeInTheDocument();
    const repo = within(source).getByRole("link", { name: /^https:\/\/github\.com\/shop\/storefront\.git/ });
    expect(repo).toHaveAttribute("href", RELEASE_APP.source);
    expect(repo).toHaveAttribute("target", "_blank");
    expect(within(source).getByText("main")).toBeInTheDocument();
    expect(within(source).getByText("npm run build")).toBeInTheDocument();
    expect(within(source).getByText("npm run start")).toBeInTheDocument();
    expect(within(source).getByText(`wasm update ${DOMAIN} --branch <branch>`)).toBeInTheDocument();
    expect(within(source).queryByText(`wasm status ${DOMAIN}`)).not.toBeInTheDocument();
  });

  it("shows a local path as plain text, not a link", async () => {
    await settingsOf({ ...RELEASE_APP, source: "/var/www/sources/shop", branch: "develop" });
    const source = section("Source and runtime");
    expect(within(source).getByText("/var/www/sources/shop")).toBeInTheDocument();
    expect(within(source).queryByRole("link", { name: /var\/www\/sources/ })).not.toBeInTheDocument();
    expect(within(source).getByText("develop")).toBeInTheDocument();
  });

  it("reads 'Not recorded' for the source, branch and start command an older app never got", async () => {
    await settingsOf(IN_PLACE_APP);
    const source = section("Source and runtime");
    expect(within(source).getAllByText("Not recorded").length).toBeGreaterThanOrEqual(3);
    expect(within(source).getByText("None")).toBeInTheDocument();
  });

  it("has nothing to build or start on a static site", async () => {
    await settingsOf(STATIC_APP);
    const source = section("Source and runtime");
    expect(within(source).getByText("Build command")).toBeInTheDocument();
    expect(within(source).getByText("None")).toBeInTheDocument();
    expect(within(source).queryByText("Start command")).not.toBeInTheDocument();
    expect(within(source).queryByText("Port")).not.toBeInTheDocument();
    expect(within(source).getByText("The web server, no process")).toBeInTheDocument();
  });

  it("shows the release serving, how many are on disk and how many are kept", async () => {
    await settingsOf(RELEASE_APP);
    const releases = section("Releases");
    expect(await within(releases).findByText("20260925-184247-2a8b7c4")).toBeInTheDocument();
    expect(within(releases).getByText("2 releases")).toBeInTheDocument();
    expect(within(releases).getByText("1 more listed whose build failed and was removed")).toBeInTheDocument();
    expect(within(releases).getByText("5 releases")).toBeInTheDocument();
    expect(within(releases).getByRole("link", { name: "Roll back from the Deployments tab" })).toHaveAttribute("href", `/apps/${DOMAIN}/deployments`);
  });

  it("has no accessibility violations", async () => {
    await settingsOf(RELEASE_APP);
    await within(section("Releases")).findByText("20260925-184247-2a8b7c4");
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

describe("resource limits", () => {
  it("starts from the app's limits and saves exactly what the form says", async () => {
    const { user, backend } = await settingsOf(RELEASE_APP, {
      "GET /api/auth/session": () => json(200, ELEVATED),
      [`PATCH /api/apps/${DOMAIN}/limits`]: () =>
        json(200, { domain: DOMAIN, memory_max_mb: 768, cpu_quota_percent: null, tasks_max: 256, units: ["shop-example-com"], restarted: true, restart_required: false }),
    });
    const limits = section("Resource limits");
    const memory = within(limits).getByRole("textbox", { name: "Memory" });
    const cpu = within(limits).getByRole("textbox", { name: "CPU" });
    expect(memory).toHaveValue("512");
    expect(cpu).toHaveValue("150");
    expect(within(limits).getByText("MemoryMax=512M CPUQuota=150% TasksMax=256")).toBeInTheDocument();
    expect(await within(limits).findByText(/up to 400 here/)).toBeInTheDocument();

    await user.clear(memory);
    await user.type(memory, "768");
    await user.clear(cpu);
    await user.click(within(limits).getByRole("checkbox", { name: /Restart now so the limits apply/ }));
    await user.click(within(limits).getByRole("button", { name: "Save limits" }));

    await waitFor(() => {
      expect(backend.callsTo(`PATCH /api/apps/${DOMAIN}/limits`)[0]?.body).toEqual({
        memory_max_mb: 768,
        cpu_quota_percent: null,
        tasks_max: 256,
        restart: true,
      });
    });
    expect(await within(limits).findByText("Saved. shop-example-com restarted under the new limits.")).toBeInTheDocument();
  });

  it("asks who it is before saving, so the save is one request", async () => {
    const { user, backend } = await settingsOf(RELEASE_APP, {
      "POST /api/auth/elevate": () => json(200, { elevated_until: "2999-01-01T00:00:00+00:00" }),
      [`PATCH /api/apps/${DOMAIN}/limits`]: () =>
        json(200, { domain: DOMAIN, memory_max_mb: 1024, cpu_quota_percent: 150, tasks_max: 256, units: ["shop-example-com"], restarted: false, restart_required: true }),
    });
    const limits = section("Resource limits");
    const memory = within(limits).getByRole("textbox", { name: "Memory" });
    await user.clear(memory);
    await user.type(memory, "1024");
    await user.click(within(limits).getByRole("button", { name: "Save limits" }));
    expect(backend.callsTo(`PATCH /api/apps/${DOMAIN}/limits`)).toHaveLength(0);
    const elevate = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(elevate).getByRole("textbox"), "123456");
    await user.click(within(elevate).getByRole("button", { name: "Confirm" }));
    expect(await within(limits).findByText(/keeps its old limits until it restarts/)).toBeInTheDocument();
    expect(within(limits).getByRole("button", { name: "Restart now" })).toBeInTheDocument();
    expect(backend.callsTo(`PATCH /api/apps/${DOMAIN}/limits`)).toHaveLength(1);
  });

  it("refuses what the backend would refuse, before asking it", async () => {
    const { user, backend } = await settingsOf(RELEASE_APP);
    const limits = section("Resource limits");
    const memory = within(limits).getByRole("textbox", { name: "Memory" });
    await user.clear(memory);
    await user.type(memory, "32");
    await user.click(within(limits).getByRole("button", { name: "Save limits" }));
    expect(await within(limits).findByText("A memory limit of 32M is too small. Allow at least 64M, or no limit.")).toBeInTheDocument();
    expect(memory).toHaveAttribute("aria-invalid", "true");
    expect(backend.callsTo(`PATCH /api/apps/${DOMAIN}/limits`)).toHaveLength(0);
  });

  it("shows the backend's refusal verbatim", async () => {
    const { user } = await settingsOf(RELEASE_APP, {
      "GET /api/auth/session": () => json(200, ELEVATED),
      [`PATCH /api/apps/${DOMAIN}/limits`]: () => problem(400, "validationerror", "A CPU quota of 350% is not possible here", { hint: "This machine has 2 CPU(s): use 1% to 200%." }),
    });
    const limits = section("Resource limits");
    const cpu = within(limits).getByRole("textbox", { name: "CPU" });
    await user.clear(cpu);
    await user.type(cpu, "350");
    await user.click(within(limits).getByRole("button", { name: "Save limits" }));
    expect(await within(limits).findByText("A CPU quota of 350% is not possible here")).toBeInTheDocument();
    expect(within(limits).getByText("This machine has 2 CPU(s): use 1% to 200%.")).toBeInTheDocument();
  });

  it("explains that a static site has nothing to limit", async () => {
    await settingsOf({ ...IN_PLACE_APP, status: "static", port: null });
    expect(within(section("Resource limits")).getByText(/there is no process of its own to limit/)).toBeInTheDocument();
    expect(within(section("Resource limits")).queryByRole("textbox")).not.toBeInTheDocument();
  });
});

describe("the deploy webhook", () => {
  it("shows a new secret once, with the URL, then forgets it", async () => {
    const app = { ...RELEASE_APP, webhook_enabled: false };
    const { user, backend } = await settingsOf(app, {
      [`GET /api/apps/${DOMAIN}`]: () => json(200, app),
      [`POST /api/apps/${DOMAIN}/webhook-secret`]: () => {
        app.webhook_enabled = true;
        return json(200, { domain: DOMAIN, secret: "s3cr3t-shown-once", hook_url: `https://panel.example.com/hooks/deploy/${DOMAIN}` });
      },
    });
    const webhook = section("Deploy webhook");
    expect(within(webhook).getByText("Disabled")).toBeInTheDocument();
    expect(within(webhook).getByText("Deliveries are refused until a secret is created.")).toBeInTheDocument();
    await user.click(within(webhook).getByRole("button", { name: "Enable webhook" }));

    const shown = await within(webhook).findByRole("region", { name: "Copy the secret now" });
    expect(within(shown).getByText("s3cr3t-shown-once")).toBeInTheDocument();
    expect(within(shown).getByText(`https://panel.example.com/hooks/deploy/${DOMAIN}`)).toBeInTheDocument();
    expect(within(shown).getByRole("button", { name: "Copy secret" })).toBeInTheDocument();
    expect(within(shown).getByRole("button", { name: "Copy payload URL" })).toBeInTheDocument();
    expect(within(webhook).getByText("Enabled")).toBeInTheDocument();
    expect(backend.callsTo(`POST /api/apps/${DOMAIN}/webhook-secret`)).toHaveLength(1);

    await user.click(within(shown).getByRole("button", { name: "Hide the secret" }));
    expect(within(webhook).queryByText("s3cr3t-shown-once")).not.toBeInTheDocument();
    // Another one replaces it, so it is asked first.
    expect(within(webhook).getByRole("button", { name: "Regenerate secret" })).toBeInTheDocument();
  });

  it("lists the deliveries and asks before replacing a secret they were signed with", async () => {
    const { user, backend } = await settingsOf({ ...RELEASE_APP, webhook_enabled: true }, {
      [`GET /api/apps/${DOMAIN}/webhook/deliveries`]: () => json(200, DELIVERIES),
      [`POST /api/apps/${DOMAIN}/webhook-secret`]: () => json(200, { domain: DOMAIN, secret: "new", hook_url: "https://x/hooks/deploy/y" }),
    });
    const webhook = section("Deploy webhook");
    const table = await within(webhook).findByRole("table");
    expect(within(table).getByText("2a8b7c4")).toBeInTheDocument();
    expect(within(table).getByText("Release did not pass its health check")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "Deploy 21" })).toHaveAttribute("href", `/apps/${DOMAIN}/deployments/21`);

    await user.click(within(webhook).getByRole("button", { name: "Regenerate secret" }));
    const dialog = await screen.findByRole("dialog", { name: "Regenerate the secret?" });
    expect(backend.callsTo(`POST /api/apps/${DOMAIN}/webhook-secret`)).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: "Regenerate secret" }));
    expect(await within(webhook).findByText("new")).toBeInTheDocument();
  });

  it("disables the webhook after asking", async () => {
    const app = { ...RELEASE_APP, webhook_enabled: true };
    const { user, backend } = await settingsOf(app, {
      [`GET /api/apps/${DOMAIN}`]: () => json(200, app),
      [`DELETE /api/apps/${DOMAIN}/webhook-secret`]: () => {
        app.webhook_enabled = false;
        return json(200, { domain: DOMAIN, enabled: false });
      },
    });
    const webhook = section("Deploy webhook");
    await user.click(within(webhook).getByRole("button", { name: "Disable webhook" }));
    const dialog = await screen.findByRole("dialog", { name: `Disable the webhook of ${DOMAIN}?` });
    await user.click(within(dialog).getByRole("button", { name: "Disable webhook" }));
    expect(await within(webhook).findByText("Disabled")).toBeInTheDocument();
    expect(within(webhook).getByText("Deliveries are refused until a secret is created.")).toBeInTheDocument();
    expect(within(webhook).getByRole("button", { name: "Enable webhook" })).toBeInTheDocument();
    expect(backend.callsTo(`DELETE /api/apps/${DOMAIN}/webhook-secret`)).toHaveLength(1);
  });
});

describe("enabling releases", () => {
  it("plans on request, shows the plan with its warnings, and migrates what was reviewed", async () => {
    const { user, backend } = await settingsOf(IN_PLACE_APP, {
      "GET /api/auth/session": () => json(200, ELEVATED),
      [`GET /api/apps/${DOMAIN}/migrate/plan`]: () => json(200, PLAN),
      [`POST /api/apps/${DOMAIN}/migrate`]: () =>
        json(202, { job_id: MIGRATE_JOB.id, status: "pending", message: `Migration of ${DOMAIN} to releases queued`, job: MIGRATE_JOB }),
      [`GET /api/jobs/${MIGRATE_JOB.id}`]: () => json(200, MIGRATE_JOB),
    });
    const releases = section("Releases");
    expect(backend.callsTo(`GET /api/apps/${DOMAIN}/migrate/plan`)).toHaveLength(0);
    await user.click(within(releases).getByRole("button", { name: "Plan the migration" }));

    expect(await within(releases).findByText(PLAN.warnings[0] ?? "")).toBeInTheDocument();
    expect(within(releases).getByText("uploads, storage")).toBeInTheDocument();
    expect(within(releases).getByText("The usual upload directories found in the tree")).toBeInTheDocument();
    expect(within(releases).getByText("shop-example-com, rewritten to run from current")).toBeInTheDocument();
    expect(within(releases).getByText("7 files, 661 B")).toBeInTheDocument();

    await user.click(within(releases).getByRole("button", { name: "Migrate to releases" }));
    const dialog = await screen.findByRole("dialog", { name: `Migrate ${DOMAIN} to releases?` });
    await user.click(within(dialog).getByRole("button", { name: "Migrate to releases" }));
    await waitFor(() => {
      expect(backend.callsTo(`POST /api/apps/${DOMAIN}/migrate`)[0]?.body).toEqual({ persist: ["uploads", "storage"] });
    });
    // Queuing only answers 202; the dialog stays open, following the job, until it ends.
    expect(await within(dialog).findByText(/Moving the tree, rewriting the unit/)).toBeInTheDocument();

    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().emit("job", {
        ...MIGRATE_JOB,
        status: "completed",
        progress: 100,
        result: {
          domain: DOMAIN,
          status: "migrated",
          release_id: "20260925-205300-nogit",
          persistent: ["uploads", "storage"],
          env_files: [".env"],
          files_before: 7,
          files_after: 7,
          bytes_before: 661,
          bytes_after: 661,
          unit_rewritten: true,
          site_rewritten: false,
          deployment_id: 40,
        },
      });
    });
    expect(await screen.findByText(/7 files before, 7 after/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: `Migrate ${DOMAIN} to releases?` })).not.toBeInTheDocument();
  });

  it("shows why a migration could not be queued", async () => {
    const { user } = await settingsOf(IN_PLACE_APP, {
      "GET /api/auth/session": () => json(200, ELEVATED),
      [`GET /api/apps/${DOMAIN}/migrate/plan`]: () => json(200, PLAN),
      [`POST /api/apps/${DOMAIN}/migrate`]: () =>
        problem(409, "app_busy", "shop.example.com: update started at 20:52:00 is still running", {
          hint: "Wait for it to finish, or follow it in Jobs",
        }),
    });
    const releases = section("Releases");
    await user.click(within(releases).getByRole("button", { name: "Plan the migration" }));
    await user.click(await within(releases).findByRole("button", { name: "Migrate to releases" }));
    const dialog = await screen.findByRole("dialog", { name: `Migrate ${DOMAIN} to releases?` });
    await user.click(within(dialog).getByRole("button", { name: "Migrate to releases" }));
    expect(await within(dialog).findByText(/update started at 20:52:00 is still running/)).toBeInTheDocument();
    expect(within(dialog).getByText("Wait for it to finish, or follow it in Jobs")).toBeInTheDocument();
  });

  it("shows why a queued migration failed, and that it was undone", async () => {
    const { user } = await settingsOf(IN_PLACE_APP, {
      "GET /api/auth/session": () => json(200, ELEVATED),
      [`GET /api/apps/${DOMAIN}/migrate/plan`]: () => json(200, PLAN),
      [`POST /api/apps/${DOMAIN}/migrate`]: () =>
        json(202, { job_id: MIGRATE_JOB.id, status: "pending", message: `Migration of ${DOMAIN} to releases queued`, job: MIGRATE_JOB }),
      [`GET /api/jobs/${MIGRATE_JOB.id}`]: () => json(200, MIGRATE_JOB),
    });
    const releases = section("Releases");
    await user.click(within(releases).getByRole("button", { name: "Plan the migration" }));
    await user.click(await within(releases).findByRole("button", { name: "Migrate to releases" }));
    const dialog = await screen.findByRole("dialog", { name: `Migrate ${DOMAIN} to releases?` });
    await user.click(within(dialog).getByRole("button", { name: "Migrate to releases" }));
    await within(dialog).findByText(/Moving the tree, rewriting the unit/);

    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().emit("job", {
        ...MIGRATE_JOB,
        status: "failed",
        error: "shop.example.com did not answer on the new layout: http://127.0.0.1:3000/ refused the connection",
      });
    });
    expect(await within(dialog).findByText(/did not answer on the new layout/)).toBeInTheDocument();
    expect(within(dialog).getByText(/WASM put everything back as it was/)).toBeInTheDocument();
  });
});

describe("deleting the app", () => {
  it("asks who it is first, then for the domain, and deletes with the chosen options", async () => {
    const { user, backend, location } = await settingsOf(IN_PLACE_APP, {
      "POST /api/auth/elevate": () => json(200, { elevated_until: "2999-01-01T00:00:00+00:00" }),
      [`DELETE /api/apps/${DOMAIN}`]: () => json(202, { job_id: "j1", status: "pending", message: `Deletion queued for ${DOMAIN}`, job: {} }),
    });
    const zone = section("Danger zone");
    await user.click(within(zone).getByRole("checkbox", { name: /Also delete its files/ }));
    await user.click(within(zone).getByRole("button", { name: "Delete application" }));

    const elevate = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(elevate).getByRole("textbox"), "123456");
    await user.click(within(elevate).getByRole("button", { name: "Confirm" }));

    const confirm = await screen.findByRole("alertdialog", { name: `Delete ${DOMAIN}` });
    expect(within(confirm).getByText(/Kept: backups and the files\./)).toBeInTheDocument();
    const action = within(confirm).getByRole("button", { name: "Delete application" });
    expect(action).toBeDisabled();
    await user.type(within(confirm).getByRole("textbox"), DOMAIN);
    await user.click(action);

    await waitFor(() => {
      expect(location().pathname).toBe("/apps");
    });
    const call = backend.callsTo(`DELETE /api/apps/${DOMAIN}`)[0];
    expect(Object.fromEntries(call?.search ?? [])).toEqual({ remove_files: "false", remove_ssl: "true" });
  });

  it("does nothing when the operator does not confirm it's them", async () => {
    const { user, backend } = await settingsOf(IN_PLACE_APP);
    await user.click(within(section("Danger zone")).getByRole("button", { name: "Delete application" }));
    const elevate = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.click(within(elevate).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Confirm it's you" })).not.toBeInTheDocument();
    });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(backend.callsTo(`DELETE /api/apps/${DOMAIN}`)).toHaveLength(0);
  });
});

describe("what the dialogs say", () => {
  it("names exactly what a deletion removes and keeps", () => {
    expect(deletionSummary({ path: "/srv/a" }, true, true)).toBe(
      "Stops the app and removes the service, the web server site, the certificate and the files in /srv/a. Kept: backups. This cannot be undone.",
    );
    expect(deletionSummary({ path: "/srv/a" }, false, false)).toBe(
      "Stops the app and removes the service and the web server site. Kept: backups, the certificate and the files. This cannot be undone.",
    );
  });

  it("describes a migration from its plan", () => {
    expect(confirmation(PLAN)).toBe(
      "The live tree becomes the first release, uploads, storage move to shared/, and the unit is rewritten to run from current. The app restarts and must pass a health check; if it does not, everything is put back as it was. Nothing is deleted.",
    );
    expect(planItems({ ...PLAN, untracked_files: ["a.txt", "b.txt"] }).at(-1)).toMatchObject({ label: "Only in the first release", value: "2 untracked files" });
  });
});

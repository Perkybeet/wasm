import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";

const APP = "shop.example.com";

const CERTS = {
  certificates: [
    {
      domain: "later.example.com",
      domains: ["later.example.com"],
      valid_until: "2026-12-01 00:00:00+00:00 (VALID: 67 days)",
      expires_on: "2026-12-01",
      days_remaining: 67,
      auto_renew: true,
      path: "/etc/letsencrypt/live/later.example.com/fullchain.pem",
      key_path: "/etc/letsencrypt/live/later.example.com/privkey.pem",
      issuer: "C = US, O = Let's Encrypt, CN = R11",
    },
    {
      domain: APP,
      domains: [APP, `www.${APP}`],
      valid_until: "2026-10-07 00:00:00+00:00 (VALID: 12 days)",
      expires_on: "2026-10-07",
      days_remaining: 12,
      auto_renew: true,
      path: `/etc/letsencrypt/live/${APP}/fullchain.pem`,
      key_path: `/etc/letsencrypt/live/${APP}/privkey.pem`,
      issuer: "C = US, O = Let's Encrypt, CN = R11",
    },
  ],
  total: 2,
};

const CERT_JOB = {
  id: "c0ffee02",
  type: "cert_create",
  name: `SSL for ${APP}`,
  description: "Extending",
  status: "running",
  progress: 20,
  total_steps: 100,
  current_step: "Extending the certificate to every domain",
  created_at: "2026-09-25T19:21:13",
  logs: [],
  metadata: { domain: APP },
};

describe("an application's Domains tab", () => {
  it("checks DNS before adding a name, then follows the certificate job", { timeout: 15_000 }, async () => {
    let domains = [{ domain: APP, kind: "primary", created_at: "2026-09-20T10:00:00+00:00" }];
    const backend = fakeBackend({
      ...signedInRoutes(),
      "GET /api/certs": () => json(200, CERTS),
      "GET /api/jobs/active": () => json(200, { jobs: [], total: 0, active: 0 }),
      [`GET /api/apps/${APP}/domains`]: () => json(200, { app: APP, domains }),
      [`GET /api/apps/${APP}/domains/blog.${APP}/dns`]: () =>
        json(200, { domain: `blog.${APP}`, expected_addresses: ["203.0.113.10"], resolved_addresses: ["198.51.100.23"], points_here: false }),
      [`POST /api/apps/${APP}/domains`]: () => {
        domains = [...domains, { domain: `blog.${APP}`, kind: "redirect", created_at: "2026-09-25T10:00:00+00:00" }];
        return json(201, { app: APP, domains, tls: true, adopted: [], certificate_job_id: CERT_JOB.id });
      },
      [`GET /api/jobs/${CERT_JOB.id}`]: () => json(200, CERT_JOB),
    });
    const { user } = renderConsole(`/apps/${APP}/domains`);
    const table = await screen.findByRole("region", { name: `Domains of ${APP}` });
    const primary = await within(table).findByRole("link", { name: /^shop\.example\.com/ });
    const row = primary.closest("tr");
    if (!row) throw new Error("no row");
    expect(within(row).getByText("Primary")).toBeInTheDocument();
    expect(within(row).getByText("Expires in 12 days")).toBeInTheDocument();
    // The certificate panel names its issuer in plain words, not the raw distinguished name.
    expect(screen.getByText(/Issued by Let's Encrypt R11\./)).toBeInTheDocument();

    // The primary can be checked, never removed.
    await user.click(within(row).getByRole("button", { name: `Actions for ${APP}` }));
    expect(await screen.findByRole("menuitem", { name: "Check DNS" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Remove" })).not.toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("button", { name: "Add domain" }));
    const dialog = await screen.findByRole("dialog", { name: `Add a domain to ${APP}` });
    await user.type(within(dialog).getByLabelText("Domain"), `Blog.${APP}`);
    await user.click(within(dialog).getByRole("radio", { name: /Redirect/ }));
    await user.click(within(dialog).getByRole("button", { name: "Check DNS" }));
    expect(await within(dialog).findByText(`blog.${APP} points somewhere else`)).toBeInTheDocument();
    expect(within(dialog).getByText("198.51.100.23")).toBeInTheDocument();
    expect(backend.callsTo(`POST /api/apps/${APP}/domains`)).toHaveLength(0);
    await expectNoAxeViolations(dialog);

    await user.click(within(dialog).getByRole("button", { name: "Add anyway" }));
    await waitFor(() => {
      expect(backend.callsTo(`POST /api/apps/${APP}/domains`)[0]?.body).toEqual({ domain: `blog.${APP}`, kind: "redirect" });
    });
    expect(await screen.findByText(`Extending the certificate to blog.${APP}`)).toBeInTheDocument();
    // The page behind the dialog is hidden from assistive technology until the dialog has closed.
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: /Add a domain/ })).not.toBeInTheDocument();
    });
    const blog = await within(table).findByRole("link", { name: /^blog\.shop\.example\.com/ });
    const blogRow = blog.closest("tr");
    if (!blogRow) throw new Error("no row");
    expect(within(blogRow).getByText("Redirect")).toBeInTheDocument();
    expect(within(blogRow).getByText("Extending the certificate")).toBeInTheDocument();
  });
});

describe("the Domains and certificates page", () => {
  it("lists certificates most urgent first and keeps the tab in the URL", async () => {
    fakeBackend({
      ...signedInRoutes(),
      "GET /api/certs": () => json(200, CERTS),
      "GET /api/jobs/active": () => json(200, { jobs: [], total: 0, active: 0 }),
      "GET /api/sites": () =>
        json(200, {
          sites: [
            {
              name: APP,
              webserver: "nginx",
              enabled: true,
              config_path: `/etc/nginx/sites-available/${APP}`,
              has_ssl: true,
              server_names: [APP, `www.${APP}`, `shop.${APP}`, `status.${APP}`],
            },
          ],
          total: 1,
          webserver: "nginx",
        }),
    });
    const harness = renderConsole("/domains");
    const table = await screen.findByRole("region", { name: "Certificates" });
    await within(table).findByText("later.example.com");
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent(APP);
    expect(rows[1]).toHaveTextContent("Expires in 12 days");
    expect(rows[1]).toHaveTextContent("Let's Encrypt R11");
    expect(rows[2]).toHaveTextContent("Valid for 67 days");

    await harness.user.click(screen.getByRole("tab", { name: /Sites/ }));
    await waitFor(() => {
      expect(harness.location().search).toEqual({ tab: "sites" });
    });
    const sites = await screen.findByRole("region", { name: "Sites" });
    expect(within(sites).getByText("Enabled")).toBeInTheDocument();
    expect(within(sites).getByText("HTTPS")).toBeInTheDocument();
    // The first three names show inline, mono, with the rest counted rather than crowding the row.
    expect(within(sites).getByText(`${APP}, www.${APP}, shop.${APP}`)).toBeInTheDocument();
    expect(within(sites).getByText("+1 more")).toBeInTheDocument();
  });
});

describe("a site's configuration editor", () => {
  it("keeps a configuration nginx rejects off the disk and shows nginx's words", async () => {
    const config = "server {\n    listen 80;\n    server_name shop.example.com\n}\n";
    const backend = fakeBackend({
      ...signedInRoutes(),
      [`GET /api/sites/${APP}`]: () =>
        json(200, { name: APP, webserver: "nginx", enabled: true, config_path: `/etc/nginx/sites-available/${APP}`, has_ssl: false, server_names: [APP] }),
      [`GET /api/sites/${APP}/config`]: () => json(200, { site: APP, webserver: "nginx", config, path: `/etc/nginx/sites-available/${APP}` }),
      [`PUT /api/sites/${APP}/config`]: () =>
        problem(400, "validationerror", `nginx rejected the configuration for ${APP}`, {
          output: `nginx: [emerg] unexpected "}" in /tmp/wasm-validate-1a2b/${APP}:4\nnginx: configuration file /tmp/wasm-validate-1a2b/wasm-validate.conf test failed\n`,
        }),
    });
    const { user } = renderConsole(`/domains/sites/${APP}`);
    const editor = await screen.findByRole("textbox", { name: `Configuration of ${APP}` });
    expect(editor).toHaveValue(config);
    expect(screen.getByRole("button", { name: "Test and save" })).toBeDisabled();

    await user.type(editor, "# edited");
    await user.click(screen.getByRole("button", { name: "Test and save" }));
    const refusal = await screen.findByText("Nothing was saved: the configuration test failed.");
    const alert = refusal.closest<HTMLElement>("[role=alert]");
    if (!alert) throw new Error("the refusal is not announced");
    expect(within(alert).getByText(/unexpected "}" in \/tmp\/wasm-validate-1a2b\/shop\.example\.com:4/)).toBeInTheDocument();
    expect(backend.callsTo(`PUT /api/sites/${APP}/config`)[0]?.body).toEqual({ config: `${config}# edited` });
    expect(editor).toHaveAttribute("aria-invalid", "true");

    await user.click(within(alert).getByRole("button", { name: "Go to line 4" }));
    expect(editor).toHaveFocus();
    expect((editor as HTMLTextAreaElement).selectionStart).toBe(config.indexOf("}\n"));
    await expectNoAxeViolations(document.body);
  });

  it("shows a reload's own refusal verbatim, after a save whose own test passed", async () => {
    const config = "server {\n    listen 80;\n    server_name shop.example.com;\n}\n";
    const backend = fakeBackend({
      ...signedInRoutes(),
      [`GET /api/sites/${APP}`]: () =>
        json(200, { name: APP, webserver: "nginx", enabled: true, config_path: `/etc/nginx/sites-available/${APP}`, has_ssl: false, server_names: [APP] }),
      [`GET /api/sites/${APP}/config`]: () => json(200, { site: APP, webserver: "nginx", config, path: `/etc/nginx/sites-available/${APP}` }),
      [`PUT /api/sites/${APP}/config`]: () => json(200, { success: true, message: `Configuration updated for ${APP}. Reload nginx to apply it.`, site: APP }),
      "POST /api/sites/reload": () =>
        problem(400, "validationerror", "nginx configuration test failed; the running config was kept", {
          output: `nginx: [emerg] duplicate listen options for [::]:8080 in /etc/nginx/sites-available/${APP}:3\nnginx: configuration file /etc/nginx/nginx.conf test failed\n`,
        }),
    });
    const { user } = renderConsole(`/domains/sites/${APP}`);
    const editor = await screen.findByRole("textbox", { name: `Configuration of ${APP}` });
    await user.type(editor, "    listen 8080;\n");
    await user.click(screen.getByRole("button", { name: "Test and save" }));
    expect(await screen.findByText(/^Saved\. It passed nginx's configuration test/)).toBeInTheDocument();
    expect(backend.callsTo(`PUT /api/sites/${APP}/config`)).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Reload nginx" }));
    const failure = await screen.findByText("nginx was not reloaded; it keeps serving the configuration from before this save.");
    const alert = failure.closest<HTMLElement>("[role=alert]");
    if (!alert) throw new Error("the reload failure is not announced");
    expect(within(alert).getByText(/duplicate listen options/)).toBeInTheDocument();
    await expectNoAxeViolations(document.body);
  });

  it("tests a candidate without saving it, either way showing nginx's own output", async () => {
    const config = "server {\n    listen 80;\n    server_name shop.example.com;\n}\n";
    const backend = fakeBackend({
      ...signedInRoutes(),
      [`GET /api/sites/${APP}`]: () =>
        json(200, {
          name: APP,
          webserver: "nginx",
          enabled: true,
          config_path: `/etc/nginx/sites-available/${APP}`,
          has_ssl: false,
          server_names: [APP, `www.${APP}`],
        }),
      [`GET /api/sites/${APP}/config`]: () => json(200, { site: APP, webserver: "nginx", config, path: `/etc/nginx/sites-available/${APP}` }),
      [`POST /api/sites/${APP}/config/test`]: (call) => {
        const content = (call.body as { content: string }).content;
        return content.includes("bad-directive")
          ? json(200, {
              ok: false,
              output: `nginx: [emerg] unknown directive "bad-directive" in /tmp/wasm-validate-9f/${APP}:4\nnginx: configuration file /tmp/wasm-validate-9f/wasm-validate.conf test failed\n`,
            })
          : json(200, {
              ok: true,
              output: "nginx: the configuration file /tmp/wasm-validate-9f/wasm-validate.conf syntax is ok\nnginx: configuration file /tmp/wasm-validate-9f/wasm-validate.conf test is successful\n",
            });
      },
    });
    const { user } = renderConsole(`/domains/sites/${APP}`);
    const editor = await screen.findByRole("textbox", { name: `Configuration of ${APP}` });
    // The names the site serves, from SiteInfo.server_names.
    expect(await screen.findByText(`${APP}, www.${APP}`)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Test" }));
    expect(await screen.findByText("This passed nginx's configuration test. Nothing was saved yet.")).toBeInTheDocument();
    expect(screen.getByText(/syntax is ok/)).toBeInTheDocument();
    expect(backend.callsTo(`PUT /api/sites/${APP}/config`)).toHaveLength(0);

    await user.type(editor, "bad-directive on;\n");
    await user.click(screen.getByRole("button", { name: "Test" }));
    const refusal = await screen.findByText("Nothing was saved: the configuration test failed.");
    const alert = refusal.closest<HTMLElement>("[role=alert]");
    if (!alert) throw new Error("the refusal is not announced");
    expect(within(alert).getByText(/unknown directive "bad-directive"/)).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Go to line 4" })).toBeInTheDocument();
    // Testing never saves: only the endpoint that tests, never the one that saves, was asked.
    expect(backend.callsTo(`PUT /api/sites/${APP}/config`)).toHaveLength(0);
    expect(backend.callsTo(`POST /api/sites/${APP}/config/test`).length).toBeGreaterThan(0);
    await expectNoAxeViolations(document.body);
  });
});

describe("creating a site", () => {
  it("offers whatever templates the API lists, not a fixed set", async () => {
    const backend = fakeBackend({
      ...signedInRoutes(),
      "GET /api/certs": () => json(200, { certificates: [], total: 0 }),
      "GET /api/sites": () => json(200, { sites: [], total: 0, webserver: "nginx" }),
      "GET /api/sites/templates": () => json(200, { templates: ["proxy", "megacorp"], webserver: "nginx" }),
      "POST /api/sites": () => json(200, { success: true, message: "Site created: status.example.com. Reload nginx to apply it.", site: "status.example.com" }),
    });
    const { user } = renderConsole("/domains?tab=sites");
    // Waits for the sites list to settle first: while it loads, the toolbar shows its own
    // "Create site" button too, a different element from the empty state's.
    await screen.findByText("No sites yet");
    await user.click(screen.getByRole("button", { name: "Create site" }));
    const dialog = await screen.findByRole("dialog", { name: "Create a site" });

    // Not a hardcoded list: an unlisted template never shows, and an unknown one still does,
    // titled from its own name.
    expect(await within(dialog).findByRole("radio", { name: /Megacorp/ })).toBeInTheDocument();
    expect(within(dialog).queryByText("Static files")).not.toBeInTheDocument();

    await user.type(within(dialog).getByLabelText("Domain"), "status.example.com");
    await user.click(within(dialog).getByRole("radio", { name: /Megacorp/ }));
    await user.click(within(dialog).getByRole("button", { name: /^Create site/ }));
    await waitFor(() => {
      expect(backend.callsTo("POST /api/sites")[0]?.body).toMatchObject({ domain: "status.example.com", template: "megacorp" });
    });
  });
});

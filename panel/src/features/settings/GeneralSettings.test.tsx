import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

function generalRoutes(extra: Record<string, RouteHandler> = {}): Record<string, RouteHandler> {
  return {
    ...signedInRoutes(),
    "GET /api/config": () => json(200, { config: {}, path: "/etc/wasm/config.yaml", writable: true }),
    "GET /api/config/apps-directory": () => json(200, { apps_directory: "/var/www/apps" }),
    "GET /api/config/webserver": () => json(200, { webserver: "nginx" }),
    "GET /api/config/ssl": () => json(200, { enabled: true, provider: "certbot", email: "ops@example.com" }),
    "GET /api/config/backup": () => json(200, { directory: "/var/backups/wasm", max_per_app: 10 }),
    "GET /api/config/web": () => json(200, { host: "127.0.0.1", port: 8080, session_timeout: 3600 }),
    ...extra,
  };
}

/** Waits for the visible toast (not its announcement) to say `text`. */
async function expectToast(text: string): Promise<void> {
  await waitFor(() => {
    expect([...document.querySelectorAll(".toast")].some((toast) => toast.textContent.includes(text))).toBe(true);
  });
}

function section(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("Settings > General", () => {
  it("shows each section's settings, where they are saved, and passes axe", { timeout: 20_000 }, async () => {
    fakeBackend(generalRoutes());
    const { container } = renderConsole("/settings");
    await screen.findByDisplayValue("/var/www/apps");
    expect(screen.getByText("/etc/wasm/config.yaml")).toBeInTheDocument();
    expect(within(section("Backups")).getByLabelText("Backups kept per application")).toHaveValue(10);
    expect(within(section("Certificates")).getByLabelText(/Email for certificate notices/)).toHaveValue("ops@example.com");
    // Nothing changed: nothing to save, and the terminal form reads the setting.
    expect(within(section("Backups")).getByRole("button", { name: "Save changes" })).toBeDisabled();
    expect(within(section("Backups")).getByText("wasm config get backup")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("shows the server's refusal beside the field it is about, and saves once it is fixed", { timeout: 20_000 }, async () => {
    let stored = { directory: "/var/backups/wasm", max_per_app: 10 };
    const backend = fakeBackend(
      generalRoutes({
        "GET /api/config/backup": () => json(200, stored),
        "PUT /api/config/backup": (call) => {
          const body = call.body as typeof stored;
          if (body.max_per_app > 100) {
            return problem(422, "validation_error", "Validation failed", {
              fields: { max_per_app: "Input should be less than or equal to 100" },
            });
          }
          stored = body;
          return json(200, { message: "Backup configuration updated" });
        },
      }),
    );
    const { user } = renderConsole("/settings");
    const retention = await screen.findByLabelText("Backups kept per application");
    const backups = section("Backups");

    await user.clear(retention);
    await user.type(retention, "500");
    expect(within(backups).getByText("Unsaved changes")).toBeInTheDocument();
    // The terminal form follows the edit.
    expect(within(backups).getByText("wasm config set backup.max_per_app 500")).toBeInTheDocument();
    await user.click(within(backups).getByRole("button", { name: "Save changes" }));

    const message = await within(backups).findByText("Input should be less than or equal to 100");
    expect(retention).toHaveAttribute("aria-invalid", "true");
    expect(retention.getAttribute("aria-describedby") ?? "").toContain(message.closest("[id]")?.id ?? "missing");
    expect(backend.callsTo("PUT /api/config/backup")[0]?.body).toEqual({ directory: "/var/backups/wasm", max_per_app: 500 });

    // Editing the field retracts the message about the value that was sent.
    await user.clear(retention);
    await user.type(retention, "12");
    expect(within(backups).queryByText("Input should be less than or equal to 100")).not.toBeInTheDocument();
    await user.click(within(backups).getByRole("button", { name: "Save changes" }));

    await expectToast("Saved the backup settings");
    await waitFor(() => {
      expect(within(backups).getByRole("button", { name: "Save changes" })).toBeDisabled();
    });
    expect(retention).toHaveValue(12);
    expect(backend.callsTo("PUT /api/config/backup")[1]?.body).toEqual({ directory: "/var/backups/wasm", max_per_app: 12 });
  });

  it("gives a one-field section a refusal that names no field, with the server's fix", async () => {
    fakeBackend(
      generalRoutes({
        "PUT /api/config/apps-directory": () =>
          problem(400, "configerror", "apps_directory must be an absolute path", {
            hint: "Got 'apps'. Use a path starting with '/', such as /var/www/apps.",
          }),
      }),
    );
    const { user } = renderConsole("/settings");
    const directory = await screen.findByLabelText("Directory");
    await user.clear(directory);
    await user.type(directory, "apps");
    await user.click(within(section("Applications directory")).getByRole("button", { name: "Save changes" }));
    expect(
      await screen.findByText(
        "apps_directory must be an absolute path Got 'apps'. Use a path starting with '/', such as /var/www/apps.",
      ),
    ).toBeInTheDocument();
    expect(directory).toHaveAttribute("aria-invalid", "true");
  });

  it("discards an edit back to the saved value", async () => {
    fakeBackend(generalRoutes());
    const { user } = renderConsole("/settings");
    const email = await screen.findByLabelText(/Email for certificate notices/);
    await user.clear(email);
    await user.type(email, "someone@example.com");
    await user.click(within(section("Certificates")).getByRole("button", { name: "Discard" }));
    expect(email).toHaveValue("ops@example.com");
    expect(within(section("Certificates")).queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("sends the certificate block whole, keeping the values the form does not show", async () => {
    const backend = fakeBackend(generalRoutes({ "PUT /api/config/ssl": () => json(200, { message: "SSL configuration updated" }) }));
    const { user } = renderConsole("/settings");
    const email = await screen.findByLabelText(/Email for certificate notices/);
    await user.clear(email);
    await user.type(email, "certs@example.com");
    await user.click(within(section("Certificates")).getByRole("button", { name: "Save changes" }));
    await expectToast("Saved the certificate email");
    expect(backend.callsTo("PUT /api/config/ssl")[0]?.body).toEqual({ enabled: true, provider: "certbot", email: "certs@example.com" });
  });
});

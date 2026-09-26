import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { SESSION, fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const NOW = Date.now() / 1000;

const SESSIONS = {
  active_sessions: 3,
  current_session: "c0ffee00aa",
  sessions: [
    { sid_prefix: "c0ffee00", client_ip: "203.0.113.7", created_at: NOW - 3600, last_seen: NOW - 5, expires_at: NOW + 11 * 3600, is_current: true },
    { sid_prefix: "a1b2c3d4", client_ip: "198.51.100.20", created_at: NOW - 86_400, last_seen: NOW - 7200, expires_at: NOW + 3600, is_current: false },
    { sid_prefix: "e5f6a7b8", client_ip: "198.51.100.21", created_at: NOW - 600, last_seen: NOW - 60, expires_at: NOW + 40_000, is_current: false },
  ],
};

const CONFIG = {
  config: {
    web: {
      max_failed_attempts: 5,
      lockout_duration: 900,
      rate_limit_enabled: true,
      rate_limit_requests: 120,
      rate_limit_window: 60,
      token_expiration_hours: 12,
      ip_whitelist: [],
    },
  },
  path: "/etc/wasm/config.yaml",
  writable: true,
};

const ENROLLMENT = {
  secret: "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
  uri: "otpauth://totp/WASM%3Aweb-01?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=WASM&algorithm=SHA1&digits=6&period=30",
};

const CODES = ["1a2b-3c4d", "5e6f-7a8b", "9c0d-1e2f", "3a4b-5c6d", "7e8f-9a0b", "1c2d-3e4f", "5a6b-7c8d", "9e0f-1a2b"];

function securityRoutes(twoFactor: { enabled: boolean }, extra: Record<string, RouteHandler> = {}): Record<string, RouteHandler> {
  return {
    ...signedInRoutes({ ...SESSION, totp_enabled: twoFactor.enabled }),
    "GET /api/auth/2fa": () => json(200, { enabled: twoFactor.enabled, pending: false, backup_codes_remaining: twoFactor.enabled ? 8 : 0 }),
    "GET /api/auth/sessions": () => json(200, SESSIONS),
    "GET /api/config": () => json(200, CONFIG),
    ...extra,
  };
}

async function expectToast(text: string): Promise<void> {
  await waitFor(() => {
    expect([...document.querySelectorAll(".toast")].some((toast) => toast.textContent.includes(text))).toBe(true);
  });
}

describe("Settings > Security", () => {
  it("sets up two-factor authentication: QR and key, a code, then the backup codes once", { timeout: 20_000 }, async () => {
    const twoFactor = { enabled: false };
    const backend = fakeBackend(
      securityRoutes(twoFactor, {
        "POST /api/auth/2fa/enroll": () => json(200, ENROLLMENT),
        "POST /api/auth/2fa/confirm": (call) => {
          if ((call.body as { code: string }).code !== "123456") {
            return problem(400, "validation_error", "That code was not accepted. Scan the QR again and enter a fresh code.");
          }
          twoFactor.enabled = true;
          return json(200, { success: true, backup_codes: CODES });
        },
      }),
    );
    const { user } = renderConsole("/settings/security");
    await user.click(await screen.findByRole("button", { name: "Set up two-factor authentication" }));

    const dialog = await screen.findByRole("dialog", { name: "Set up two-factor authentication" });
    const qr = within(dialog).getByRole("img", { name: /QR code to add WASM \(web-01\)/ });
    // Drawn by React as a path, never markup injected into the page.
    expect(qr.tagName.toLowerCase()).toBe("svg");
    expect(qr.querySelector("path")?.getAttribute("d")).toMatch(/^M\d/);
    expect(within(dialog).getByText("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP")).toBeInTheDocument();
    await expectNoAxeViolations(dialog);

    const code = within(dialog).getByLabelText("Authentication code");
    expect(code).toHaveFocus();
    await user.type(code, "000000");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));
    expect(
      await within(dialog).findByText("That code was not accepted. Scan the QR again and enter a fresh code."),
    ).toBeInTheDocument();

    await user.clear(code);
    await user.type(code, "123456");
    await user.click(within(dialog).getByRole("button", { name: "Turn on" }));

    const codesDialog = await screen.findByRole("dialog", { name: "Save your backup codes" });
    const list = within(codesDialog).getByRole("list", { name: "Backup codes" });
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual(CODES);
    const done = within(codesDialog).getByRole("button", { name: "Done" });
    expect(done).toBeDisabled();

    // Closing before saving them says why it did not close.
    await user.keyboard("{Escape}");
    expect(await within(codesDialog).findByRole("alert")).toHaveTextContent("They cannot be shown again");
    expect(screen.getByRole("dialog", { name: "Save your backup codes" })).toBeInTheDocument();

    await user.click(within(codesDialog).getByRole("checkbox", { name: "I have saved these codes somewhere safe" }));
    await user.click(done);
    await expectToast("Turned on two-factor authentication");
    expect(await screen.findByText("8 of 8 backup codes left")).toBeInTheDocument();
    expect(backend.callsTo("POST /api/auth/2fa/confirm").map((call) => call.body)).toEqual([{ code: "000000" }, { code: "123456" }]);
  });

  it("asks to confirm it's you before starting setup, and opens the enrolment dialog only after", { timeout: 20_000 }, async () => {
    const twoFactor = { enabled: false };
    let elevated = false;
    fakeBackend(
      securityRoutes(twoFactor, {
        "POST /api/auth/2fa/enroll": () => {
          if (!elevated) return problem(403, "elevation_required", "Confirm it's you to continue.");
          return json(200, ENROLLMENT);
        },
        "POST /api/auth/elevate": (call) => {
          if ((call.body as { token: string }).token !== "wasm_mastertoken") {
            return problem(401, "invalid_credential", "That token was not accepted.");
          }
          elevated = true;
          return json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() });
        },
      }),
    );
    const { user } = renderConsole("/settings/security");
    await user.click(await screen.findByRole("button", { name: "Set up two-factor authentication" }));

    // Two-factor is off, so elevation is done with the access token, not a code (D5).
    const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
    expect(confirm).toHaveAccessibleDescription(/access token/i);
    expect(screen.queryByRole("dialog", { name: "Set up two-factor authentication" })).not.toBeInTheDocument();
    await user.type(within(confirm).getByLabelText("Access token"), "wasm_mastertoken");
    await user.click(within(confirm).getByRole("button", { name: "Confirm" }));

    // Only once elevated does the enrolment dialog open, with the secret fetched after the retry.
    const dialog = await screen.findByRole("dialog", { name: "Set up two-factor authentication" });
    expect(screen.queryByRole("dialog", { name: "Confirm it's you" })).not.toBeInTheDocument();
    expect(within(dialog).getByText("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP")).toBeInTheDocument();
    await expectNoAxeViolations(dialog);
  });

  it("turns it off with a code, confirming it's you first when the server asks", { timeout: 20_000 }, async () => {
    const twoFactor = { enabled: true };
    let elevated = false;
    const backend = fakeBackend(
      securityRoutes(twoFactor, {
        "POST /api/auth/2fa/disable": () => {
          if (!elevated) return problem(403, "elevation_required", "Confirm it's you to continue.");
          twoFactor.enabled = false;
          return json(200, { success: true, message: "Two-factor authentication disabled" });
        },
        "POST /api/auth/elevate": () => {
          elevated = true;
          return json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() });
        },
      }),
    );
    const { user } = renderConsole("/settings/security");
    await user.click(await screen.findByRole("button", { name: "Turn off" }));
    const dialog = await screen.findByRole("dialog", { name: "Turn off two-factor authentication" });
    await user.type(within(dialog).getByLabelText("Authentication or backup code"), "654321");
    await user.click(within(dialog).getByRole("button", { name: "Turn off" }));

    const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(confirm).getByLabelText("Authentication code"), "654321");
    await user.click(within(confirm).getByRole("button", { name: "Confirm" }));

    await expectToast("Turned off two-factor authentication");
    expect(await screen.findByRole("button", { name: "Set up two-factor authentication" })).toBeInTheDocument();
    expect(backend.callsTo("POST /api/auth/2fa/disable").map((call) => call.body)).toEqual([{ code: "654321" }, { code: "654321" }]);
    expect(backend.callsTo("POST /api/auth/elevate")[0]?.body).toEqual({ code: "654321" });
  });

  it("replaces the backup codes after asking, confirming it's you, and shows the new set once", { timeout: 20_000 }, async () => {
    const NEW_CODES = CODES.map((code) => code.split("").reverse().join(""));
    let elevated = false;
    const backend = fakeBackend(
      securityRoutes({ enabled: true }, {
        "POST /api/auth/2fa/backup-codes": () => {
          if (!elevated) return problem(403, "elevation_required", "Confirm it's you to continue.");
          return json(200, { success: true, backup_codes: NEW_CODES });
        },
        "POST /api/auth/elevate": () => {
          elevated = true;
          return json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() });
        },
      }),
    );
    const { user } = renderConsole("/settings/security");
    await user.click(await screen.findByRole("button", { name: "New backup codes" }));
    const ask = await screen.findByRole("dialog", { name: "Replace your backup codes?" });
    expect(ask).toHaveAccessibleDescription(/old ones stop working/);
    await user.click(within(ask).getByRole("button", { name: "Replace backup codes" }));

    const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(confirm).getByLabelText("Authentication code"), "654321");
    await user.click(within(confirm).getByRole("button", { name: "Confirm" }));

    const shown = await screen.findByRole("dialog", { name: "Save your backup codes" });
    const list = within(shown).getByRole("list", { name: "Backup codes" });
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual(NEW_CODES);
    // Done waits for the box: closing now would lose the only copy.
    expect(within(shown).getByRole("button", { name: "Done" })).toBeDisabled();
    await user.click(within(shown).getByRole("checkbox", { name: "I have saved these codes somewhere safe" }));
    await user.click(within(shown).getByRole("button", { name: "Done" }));
    await expectToast("Replaced the backup codes");
    expect(backend.callsTo("POST /api/auth/2fa/backup-codes")).toHaveLength(2);
  });

  it("lists the sessions with this browser marked, and signs the others out", { timeout: 20_000 }, async () => {
    const backend = fakeBackend(
      securityRoutes({ enabled: true }, {
        "DELETE /api/auth/sessions/a1b2c3d4": () => json(200, { success: true, revoked: "a1b2c3d4" }),
        "POST /api/auth/sessions/revoke-others": () => json(200, { success: true, message: "Revoked 1 other session(s)" }),
      }),
    );
    const { user, container } = renderConsole("/settings/security");
    const current = (await screen.findByText("c0ffee00")).closest("tr");
    const table = screen.getByRole("region", { name: "Active sessions" });
    if (!current) throw new Error("no row");
    expect(within(current).getByText("This browser")).toBeInTheDocument();
    expect(within(current).queryByRole("button", { name: /Sign out/ })).not.toBeInTheDocument();
    await expectNoAxeViolations(container);

    await user.click(within(table).getByRole("button", { name: "Sign out session a1b2c3d4" }));
    await expectToast("Signed out session a1b2c3d4");

    await user.click(screen.getByRole("button", { name: "Sign out other sessions" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out other sessions?" });
    await expectNoAxeViolations(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Sign out other sessions" }));
    await expectToast("Signed out 1 other session");
    expect(screen.queryByRole("dialog", { name: "Sign out other sessions?" })).not.toBeInTheDocument();
    expect(backend.calls.filter((call) => call.method === "DELETE").map((call) => call.path)).toEqual([
      "/api/auth/sessions/a1b2c3d4",
    ]);
    expect(backend.callsTo("POST /api/auth/sessions/revoke-others")).toHaveLength(1);
  });

  it("shows a token credential's refusal to sign out other sessions, verbatim with a hint", async () => {
    fakeBackend(
      securityRoutes({ enabled: true }, {
        "POST /api/auth/sessions/revoke-others": () =>
          problem(400, "validation_error", "This credential is not a browser session; there is no other session to leave signed in."),
      }),
    );
    const { user } = renderConsole("/settings/security");
    await user.click(await screen.findByRole("button", { name: "Sign out other sessions" }));
    const dialog = await screen.findByRole("dialog", { name: "Sign out other sessions?" });
    await user.click(within(dialog).getByRole("button", { name: "Sign out other sessions" }));

    expect(
      await within(dialog).findByText("This credential is not a browser session; there is no other session to leave signed in."),
    ).toBeInTheDocument();
    expect(within(dialog).getByText(/Sign in through the browser/)).toBeInTheDocument();
    // The dialog stays open: nothing was signed out, so there is nothing to dismiss it for.
    expect(screen.getByRole("dialog", { name: "Sign out other sessions?" })).toBeInTheDocument();
  });

  it("states the lockout policy as configured", async () => {
    fakeBackend(securityRoutes({ enabled: true }));
    renderConsole("/settings/security");
    const policy = await screen.findByRole("region", { name: "Lockout policy" });
    expect(await within(policy).findByText("5")).toBeInTheDocument();
    expect(within(policy).getByText("15 minutes")).toBeInTheDocument();
    expect(within(policy).getByText("120 requests a minute")).toBeInTheDocument();
    expect(within(policy).getByText("12 hours")).toBeInTheDocument();
    expect(within(policy).getByText("Any address")).toBeInTheDocument();
  });
});

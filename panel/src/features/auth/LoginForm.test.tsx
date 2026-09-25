import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { ANONYMOUS, SESSION, fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RecordedCall } from "../../test/fakes";

const TOKEN = "wasm_secret_token";
const CODE = "123456";

/** A backend with two-factor on: a signed-out session until a login succeeds. */
function twoFactorBackend() {
  let signedIn = false;
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/auth/session": () => json(200, signedIn ? SESSION : ANONYMOUS),
    "POST /api/auth/login": (call: RecordedCall) => {
      const body = call.body as { token: string; totp_code?: string };
      if (body.token !== TOKEN) return problem(401, "invalid_token", "Invalid token. 4 attempts remaining.");
      if (!body.totp_code) return problem(401, "totp_required", "Two-factor authentication is enabled. Include totp_code.");
      if (body.totp_code !== CODE) return problem(401, "invalid_totp", "Invalid two-factor code. 3 attempts remaining.");
      signedIn = true;
      return json(200, { success: true, expires_in: 28_800, csrf_token: "c", session_token: null });
    },
  });
  return backend;
}

describe("sign-in", () => {
  it("asks for the second factor after the token, then opens the page that was asked for", async () => {
    const backend = twoFactorBackend();
    const { user, location } = renderConsole("/login?next=%2Fapps%2Fshop.example.com%2Flogs");

    const token = await screen.findByLabelText("Access token");
    await waitFor(() => {
      expect(token).toHaveFocus();
    });
    await user.type(token, TOKEN);
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    const code = await screen.findByLabelText("Two-factor code");
    await waitFor(() => {
      expect(code).toHaveFocus();
    });
    expect(screen.getByText("accepted")).toBeInTheDocument();
    await user.type(code, CODE);
    await user.click(screen.getByRole("button", { name: "Verify" }));

    await screen.findByRole("heading", { level: 1, name: "shop.example.com" });
    expect(location().pathname).toBe("/apps/shop.example.com/logs");
    expect(backend.callsTo("POST /api/auth/login").map((call) => call.body)).toEqual([
      { token: TOKEN, bearer: false },
      { token: TOKEN, bearer: false, totp_code: CODE },
    ]);
  });

  it("shows the server's words when the token is wrong", async () => {
    twoFactorBackend();
    const { user } = renderConsole("/login");
    await user.type(await screen.findByLabelText("Access token"), "nope");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Invalid token. 4 attempts remaining.")).toBeInTheDocument();
    expect(screen.getByLabelText("Access token")).toHaveAttribute("aria-invalid", "true");
  });

  it("keeps the second step open when the code is wrong, and can go back to the token", async () => {
    twoFactorBackend();
    const { user } = renderConsole("/login");
    await user.type(await screen.findByLabelText("Access token"), TOKEN);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await user.type(await screen.findByLabelText("Two-factor code"), "000000");
    await user.click(screen.getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("Invalid two-factor code. 3 attempts remaining.")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Use a different token" }));
    expect(await screen.findByLabelText("Access token")).toBeInTheDocument();
    expect(screen.queryByLabelText("Two-factor code")).toBeNull();
  });

  it("says how long a lockout lasts and holds the form until it ends", async () => {
    fakeBackend({
      "GET /api/auth/session": () => json(200, ANONYMOUS),
      "POST /api/auth/login": () =>
        problem(429, "locked_out", "Too many failed attempts. Locked for 125 seconds.", { headers: { "Retry-After": "125" } }),
    });
    const { user } = renderConsole("/login");
    await user.type(await screen.findByLabelText("Access token"), TOKEN);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Too many failed attempts")).toBeInTheDocument();
    expect(screen.getByText("2:05")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("never follows a next that leaves the console", async () => {
    let signedIn = false;
    fakeBackend({
      ...signedInRoutes(),
      "GET /api/auth/session": () => json(200, signedIn ? SESSION : { ...ANONYMOUS, totp_enabled: false }),
      "POST /api/auth/login": () => {
        signedIn = true;
        return json(200, { success: true, expires_in: 60, csrf_token: "c", session_token: null });
      },
    });
    const { user, location } = renderConsole("/login?next=%2F%2Fevil.example%2Fphish");
    await user.type(await screen.findByLabelText("Access token"), TOKEN);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { level: 1, name: "Overview" });
    expect(location().pathname).toBe("/");
  });

  it("sends an operator who is already signed in straight on", async () => {
    fakeBackend(signedInRoutes());
    const { location } = renderConsole("/login?next=%2Fbackups");
    await screen.findByRole("heading", { level: 1, name: "Backups" });
    expect(location().pathname).toBe("/backups");
  });

  it("names the machine before anything else", async () => {
    twoFactorBackend();
    renderConsole("/login");
    expect(await screen.findByText("web-01")).toBeInTheDocument();
    expect(document.title).toBe("Sign in - web-01 - WASM");
  });

  it("has no accessibility violations on either step", async () => {
    twoFactorBackend();
    const { user } = renderConsole("/login?reason=expired");
    await screen.findByText("web-01");
    await expectNoAxeViolations(document.body, { page: true });
    await user.type(screen.getByLabelText("Access token"), TOKEN);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByLabelText("Two-factor code");
    await expectNoAxeViolations(document.body, { page: true });
  });
});

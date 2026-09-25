import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ElevationCancelledError, api } from "../../api/client";
import { authKeys } from "../../api/queries/auth";
import type { SessionInfo } from "../../api/queries/auth";
import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { SESSION, fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RecordedCall } from "../../test/fakes";

const UNTIL = "2026-09-25T18:10:00+00:00";

function backendNeedingElevation(session: SessionInfo = SESSION) {
  let elevated = false;
  const backend = fakeBackend({
    ...signedInRoutes(session),
    "POST /api/auth/elevate": (call: RecordedCall) => {
      const body = call.body as { code?: string; token?: string };
      const ok = session.totp_enabled ? body.code === "123456" : body.token === "wasm_token";
      if (!ok) {
        return session.totp_enabled
          ? problem(401, "invalid_totp", "Invalid two-factor code. 4 attempts remaining.")
          : problem(401, "invalid_token", "Invalid token. 4 attempts remaining.");
      }
      elevated = true;
      return json(200, { elevated_until: UNTIL });
    },
    "DELETE /api/apps/shop.example.com": () =>
      elevated
        ? json(202, { job_id: "j1", status: "pending", message: "Deleting shop.example.com", job: {} })
        : problem(403, "elevation_required", "Confirm it's you to continue"),
  });
  return backend;
}

async function consoleOn(backend: ReturnType<typeof backendNeedingElevation>) {
  const harness = renderConsole("/apps");
  await screen.findByRole("heading", { level: 1, name: "Applications" });
  return { ...harness, backend };
}

describe("Confirm it's you", () => {
  it("opens when an action needs it, and retries the action once confirmed", async () => {
    const { user, backend, queryClient } = await consoleOn(backendNeedingElevation());
    const deletion = api("DELETE", "/api/apps/shop.example.com");

    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    const code = within(dialog).getByLabelText("Authentication code");
    await waitFor(() => {
      expect(code).toHaveFocus();
    });
    await user.type(code, "123456");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));

    await expect(deletion).resolves.toMatchObject({ job_id: "j1" });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Confirm it's you" })).toBeNull();
    });
    expect(backend.callsTo("POST /api/auth/elevate").map((call) => call.body)).toEqual([{ code: "123456" }]);
    expect(backend.callsTo("DELETE /api/apps/shop.example.com")).toHaveLength(2);
    expect(queryClient.getQueryData<SessionInfo>(authKeys.session)?.elevated_until).toBe(UNTIL);
  });

  it("stays open with the server's words when the code is wrong", async () => {
    const { user, backend } = await consoleOn(backendNeedingElevation());
    const deletion = api("DELETE", "/api/apps/shop.example.com").catch((error: unknown) => error);
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(dialog).getByLabelText("Authentication code"), "111111");
    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    expect(await within(dialog).findByText("Invalid two-factor code. 4 attempts remaining.")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Confirm it's you" })).toBeInTheDocument();
    expect(backend.callsTo("DELETE /api/apps/shop.example.com")).toHaveLength(1);

    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await expect(deletion).resolves.toBeInstanceOf(ElevationCancelledError);
  });

  it("fails the action with a clear error when cancelled, without retrying it", async () => {
    const { user, backend } = await consoleOn(backendNeedingElevation());
    const deletion = api("DELETE", "/api/apps/shop.example.com").catch((error: unknown) => error);
    await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.keyboard("{Escape}");
    const error = await deletion;
    expect(error).toBeInstanceOf(ElevationCancelledError);
    expect((error as ElevationCancelledError).detail).toBe("Nothing was changed because the confirmation was cancelled.");
    expect(backend.callsTo("DELETE /api/apps/shop.example.com")).toHaveLength(1);
    expect(backend.callsTo("POST /api/auth/elevate")).toHaveLength(0);
  });

  it("asks for the access token when two-factor authentication is off", async () => {
    const { user, backend } = await consoleOn(backendNeedingElevation({ ...SESSION, totp_enabled: false }));
    const deletion = api("DELETE", "/api/apps/shop.example.com");
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    const token = within(dialog).getByLabelText("Access token");
    expect(token).toHaveAttribute("type", "password");
    await user.type(token, "wasm_token{Enter}");
    await expect(deletion).resolves.toMatchObject({ job_id: "j1" });
    expect(backend.callsTo("POST /api/auth/elevate").map((call) => call.body)).toEqual([{ token: "wasm_token" }]);
  });

  it("has no accessibility violations", async () => {
    const { user } = await consoleOn(backendNeedingElevation());
    const deletion = api("DELETE", "/api/apps/shop.example.com").catch(() => undefined);
    const dialog = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await expectNoAxeViolations(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await deletion;
  });
});

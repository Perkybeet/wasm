import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { api } from "../../api/client";
import { appKeys } from "../../api/queries/apps";
import { authKeys } from "../../api/queries/auth";
import { renderConsole } from "../../test/console";
import { ANONYMOUS, SESSION, fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import { safeNext } from "./session";

describe("safeNext", () => {
  it.each([
    ["/apps/shop.example.com/logs?follow=1", "/apps/shop.example.com/logs?follow=1"],
    ["/", "/"],
    [undefined, "/"],
    ["", "/"],
    ["https://evil.example", "/"],
    ["//evil.example/x", "/"],
    ["/\\evil.example", "/"],
    ["javascript:alert(1)", "/"],
    ["/login?next=/apps", "/"],
  ])("%s goes to %s", (next, expected) => {
    expect(safeNext(next)).toBe(expected);
  });
});

/**
 * Review Focus: a session that expires while the console is open. The next request must land
 * on the sign-in page with a "session expired" notice and return to the same URL after
 * signing in, never a blank page or a raw 401.
 */
describe("a session that expires while the console is open", () => {
  function expiringBackend() {
    let alive = true;
    const backend = fakeBackend({
      ...signedInRoutes(),
      "GET /api/auth/session": () => json(200, alive ? SESSION : { ...ANONYMOUS, totp_enabled: false }),
      "POST /api/auth/login": () => {
        alive = true;
        return json(200, { success: true, expires_in: 60, csrf_token: "c", session_token: null });
      },
    });
    const expire = () => {
      alive = false;
      backend.on("GET /api/apps", () => problem(401, "unauthorized", "Authentication required"));
    };
    return { backend, expire, revive: () => backend.on("GET /api/apps", () => json(200, { apps: [], total: 0 })) };
  }

  it("lands on sign-in with the notice, then returns to the same page", async () => {
    const { expire, revive } = expiringBackend();
    const path = "/apps/shop.example.com/environment";
    const { user, location, queryClient } = renderConsole(path);
    await screen.findByText("Environment variables");
    queryClient.setQueryData(appKeys.list, { apps: [], total: 0 });

    expire();
    await act(async () => {
      await api("GET", "/api/apps").catch(() => undefined);
    });

    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(location().pathname).toBe("/login");
    expect(location().search).toEqual({ next: path, reason: "expired" });
    // On the page, and said once through the polite live region.
    expect(
      within(screen.getByRole("main")).getByText("Your session expired. Sign in again to continue where you left off."),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByTestId("announcer-polite")).toHaveTextContent("Your session expired.");
    });
    // What the dead session loaded is gone with it.
    expect(queryClient.getQueryData(appKeys.list)).toBeUndefined();

    revive();
    await user.type(screen.getByLabelText("Access token"), "wasm_token");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByText("Environment variables");
    expect(location().pathname).toBe(path);
  });

  it("is noticed when the tab comes back, before the operator clicks anything", async () => {
    const { expire } = expiringBackend();
    const { location, queryClient } = renderConsole("/databases");
    await screen.findByRole("heading", { level: 1, name: "Databases" });

    expire();
    // What a window regaining focus does: the session query refetches.
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: authKeys.session });
    });

    await waitFor(() => {
      expect(location().pathname).toBe("/login");
    });
    expect(location().search).toEqual({ next: "/databases", reason: "expired" });
  });

  it("redirects once when several requests fail together", async () => {
    const { expire } = expiringBackend();
    const { history } = renderConsole("/apps");
    await screen.findByRole("heading", { level: 1, name: "Applications" });
    const before = history.length;
    expire();
    await act(async () => {
      await Promise.all([1, 2, 3].map(() => api("GET", "/api/apps").catch(() => undefined)));
    });
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    // Replace, not push: Back does not return to a page that can only fail again.
    expect(history.length).toBe(before);
  });
});

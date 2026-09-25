import { describe, expect, it, vi } from "vitest";

import { fakeBackend, json, problem } from "../test/fakes";
import { ApiError, ElevationCancelledError, api, buildPath, configureApi, request } from "./client";

describe("api", () => {
  it("sends JSON and returns the decoded body", async () => {
    const backend = fakeBackend({ "POST /api/things": (call) => json(200, { echoed: call.body }) });
    await expect(api("POST", "/api/things", { name: "shop" })).resolves.toEqual({ echoed: { name: "shop" } });
    const [call] = backend.calls;
    expect(call?.headers.get("Content-Type")).toBe("application/json");
    expect(call?.headers.get("Accept")).toBe("application/json");
  });

  it("mirrors the wasm_csrf cookie into the X-WASM-CSRF header", async () => {
    document.cookie = "wasm_csrf=token-from-cookie; path=/";
    const backend = fakeBackend({ "DELETE /api/apps/shop.example.com": () => json(202, { job_id: "j1" }) });
    await api("DELETE", "/api/apps/shop.example.com");
    expect(backend.calls[0]?.headers.get("X-WASM-CSRF")).toBe("token-from-cookie");
  });

  it("sends no CSRF header when there is no cookie to mirror", async () => {
    const backend = fakeBackend({ "GET /api/apps": () => json(200, { apps: [], total: 0 }) });
    await api("GET", "/api/apps");
    expect(backend.calls[0]?.headers.has("X-WASM-CSRF")).toBe(false);
  });

  it("surfaces a 422's fields for the form to show next to each control", async () => {
    fakeBackend({
      "POST /api/apps": () =>
        problem(422, "validation_error", "Validation failed", { fields: { port: "must be between 1 and 65535" } }),
    });
    const error = await api("POST", "/api/apps", { port: 0 }).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 422,
      error: "validation_error",
      detail: "Validation failed",
      fields: { port: "must be between 1 and 65535" },
    });
  });

  it("keeps the backend's detail and hint verbatim", async () => {
    fakeBackend({
      "POST /api/sites/example.com/enable": () =>
        problem(500, "webservererror", "nginx: [emerg] unknown directive \"proxy_pas\"", {
          hint: "Fix the site configuration, then reload.",
        }),
    });
    await expect(api("POST", "/api/sites/example.com/enable")).rejects.toMatchObject({
      detail: 'nginx: [emerg] unknown directive "proxy_pas"',
      hint: "Fix the site configuration, then reload.",
    });
  });

  it("reports a proxy's non-JSON error page as it was written", async () => {
    fakeBackend({ "GET /api/apps": () => new Response("<h1>502 Bad Gateway</h1>", { status: 502 }) });
    await expect(api("GET", "/api/apps")).rejects.toMatchObject({
      status: 502,
      error: "internal",
      detail: "<h1>502 Bad Gateway</h1>",
    });
  });

  it("turns an unreachable server into an ApiError with a fix", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    const error = await api("GET", "/api/apps").catch((caught: unknown) => caught);
    expect(error).toMatchObject({ status: 0, error: "network", detail: "Failed to fetch" });
    expect((error as ApiError).hint).toContain("wasm web status");
  });

  it("reads Retry-After from a lockout", async () => {
    fakeBackend({
      "POST /api/auth/login": () =>
        problem(429, "locked_out", "Too many failed attempts. Locked for 240 seconds.", {
          headers: { "Retry-After": "240" },
        }),
    });
    await expect(api("POST", "/api/auth/login", { token: "x" })).rejects.toMatchObject({
      error: "locked_out",
      retryAfter: 240,
    });
  });

  describe("a lost session", () => {
    it("is reported once per 401 that is not a failed credential", async () => {
      const onSessionExpired = vi.fn();
      configureApi({ onSessionExpired });
      fakeBackend({ "GET /api/apps": () => problem(401, "unauthorized", "Authentication required") });
      await expect(api("GET", "/api/apps")).rejects.toMatchObject({ status: 401, sessionExpired: true });
      expect(onSessionExpired).toHaveBeenCalledTimes(1);
    });

    it.each(["invalid_token", "totp_required", "invalid_totp"])("is not what a %s answer means", async (code) => {
      const onSessionExpired = vi.fn();
      configureApi({ onSessionExpired });
      fakeBackend({ "POST /api/auth/login": () => problem(401, code, "Wrong credentials") });
      await expect(api("POST", "/api/auth/login", { token: "x" })).rejects.toMatchObject({ error: code });
      expect(onSessionExpired).not.toHaveBeenCalled();
    });
  });

  describe("an action that needs elevation", () => {
    const refusal = () =>
      problem(403, "elevation_required", "Confirm it's you to continue", {
        hint: "POST /api/auth/elevate with your two-factor code, or the master token.",
      });

    it("asks the operator to confirm, then retries exactly once", async () => {
      const elevate = vi.fn(() => Promise.resolve());
      configureApi({ elevate });
      let attempts = 0;
      const backend = fakeBackend({
        "DELETE /api/apps/shop.example.com": () => {
          attempts += 1;
          return attempts === 1 ? refusal() : json(202, { job_id: "j1", status: "pending" });
        },
      });
      await expect(api("DELETE", "/api/apps/shop.example.com")).resolves.toMatchObject({ job_id: "j1" });
      expect(elevate).toHaveBeenCalledTimes(1);
      expect(backend.callsTo("DELETE /api/apps/shop.example.com")).toHaveLength(2);
    });

    it("reports a second refusal instead of asking again", async () => {
      const elevate = vi.fn(() => Promise.resolve());
      configureApi({ elevate });
      const backend = fakeBackend({ "DELETE /api/apps/shop.example.com": refusal });
      await expect(api("DELETE", "/api/apps/shop.example.com")).rejects.toMatchObject({ error: "elevation_required" });
      expect(elevate).toHaveBeenCalledTimes(1);
      expect(backend.calls).toHaveLength(2);
    });

    it("fails with a clear error and does not retry when the operator cancels", async () => {
      configureApi({ elevate: () => Promise.reject(new ElevationCancelledError()) });
      const backend = fakeBackend({ "DELETE /api/apps/shop.example.com": refusal });
      const error = await api("DELETE", "/api/apps/shop.example.com").catch((caught: unknown) => caught);
      expect(error).toBeInstanceOf(ElevationCancelledError);
      expect(error).toMatchObject({ error: "elevation_cancelled", detail: "Nothing was changed because the confirmation was cancelled." });
      expect(backend.calls).toHaveLength(1);
    });

    it("asks once for several refusals in flight together", async () => {
      let confirm!: () => void;
      const elevate = vi.fn(
        () =>
          new Promise<void>((resolve) => {
            confirm = resolve;
          }),
      );
      configureApi({ elevate });
      const confirmed = new Set<string>();
      fakeBackend({
        "DELETE /api/databases/databases/postgresql/shop": () =>
          confirmed.has("db") ? json(200, { ok: true }) : (confirmed.add("db"), refusal()),
        "DELETE /api/services/worker": () => (confirmed.has("svc") ? json(200, { ok: true }) : (confirmed.add("svc"), refusal())),
      });
      const both = Promise.all([
        api("DELETE", "/api/databases/databases/postgresql/shop"),
        api("DELETE", "/api/services/worker"),
      ]);
      await vi.waitFor(() => {
        expect(elevate).toHaveBeenCalled();
      });
      await new Promise((resolve) => setTimeout(resolve, 10));
      confirm();
      await expect(both).resolves.toEqual([{ ok: true }, { ok: true }]);
      expect(elevate).toHaveBeenCalledTimes(1);
    });
  });
});

describe("request", () => {
  it("fills path parameters encoded and appends the query", async () => {
    const backend = fakeBackend({
      "GET /api/apps/shop.example.com/logs": () => json(200, { domain: "shop.example.com", lines: 50, used: 50 }),
    });
    const logs = await request("get", "/api/apps/{domain}/logs", { params: { domain: "shop.example.com" }, query: { lines: 50 } });
    expect(logs.domain).toBe("shop.example.com");
    expect(backend.calls[0]?.search.get("lines")).toBe("50");
  });

  it("is checked against the contract at compile time", () => {
    // Nothing runs: these lines exist for the type checker, which must reject each of them.
    const never = (): void => {
      // @ts-expect-error - an endpoint the backend does not declare
      void request("get", "/api/does-not-exist");
      // @ts-expect-error - a path parameter is missing
      void request("get", "/api/apps/{domain}");
      // @ts-expect-error - a method the path does not declare
      void request("patch", "/api/apps");
    };
    expect(typeof never).toBe("function");
  });
});

describe("buildPath", () => {
  it("encodes parameters so a value cannot add a path segment", () => {
    expect(buildPath("/api/services/{name}", { name: "a/b c" })).toBe("/api/services/a%2Fb%20c");
  });

  it("drops empty query values and repeats arrays", () => {
    expect(buildPath("/api/jobs", undefined, { status: undefined, domain: null, tag: ["a", "b"], limit: 5 })).toBe(
      "/api/jobs?tag=a&tag=b&limit=5",
    );
  });
});

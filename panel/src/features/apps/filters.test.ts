import { describe, expect, it } from "vitest";

import type { AppInfo } from "./data";
import { appTypes, filterApps, isFiltered, validateAppsSearch } from "./filters";

function app(domain: string, status: string, type: string | null): AppInfo {
  return { domain, name: domain, status, active: status === "running", enabled: true, app_type: type, layout: "inplace" };
}

const APPS = [
  app("picconia.com", "running", "nextjs"),
  app("cittek.es", "stopped", "nextjs"),
  app("bodas.arennalabs.com", "static", "static"),
  app("api.arennalabs.com", "Restarting", "python"),
  app("legacy.example.com", "running", null),
];

describe("validateAppsSearch", () => {
  it("keeps valid filters", () => {
    expect(validateAppsSearch({ q: " shop ", state: "failed", type: "nextjs" })).toEqual({ q: "shop", state: "failed", type: "nextjs" });
  });

  it("drops what is malformed instead of failing the page", () => {
    expect(validateAppsSearch({ q: "", state: "exploded", type: "next js; rm", extra: 1 })).toEqual({});
    expect(validateAppsSearch({ q: 42, state: ["running"] })).toEqual({});
  });

  it("bounds free text", () => {
    expect(validateAppsSearch({ q: "x".repeat(500) }).q).toHaveLength(200);
  });
});

describe("filterApps", () => {
  it("matches the domain or the type, case-insensitively", () => {
    expect(filterApps(APPS, { q: "ARENNA" }).map((a) => a.domain)).toEqual(["bodas.arennalabs.com", "api.arennalabs.com"]);
    expect(filterApps(APPS, { q: "python" }).map((a) => a.domain)).toEqual(["api.arennalabs.com"]);
  });

  it("filters by the drawn state, whatever word the backend used", () => {
    expect(filterApps(APPS, { state: "running" }).map((a) => a.domain)).toEqual(["picconia.com", "legacy.example.com"]);
    // "Restarting" is drawn as in progress.
    expect(filterApps(APPS, { state: "deploying" }).map((a) => a.domain)).toEqual(["api.arennalabs.com"]);
  });

  it("combines filters", () => {
    expect(filterApps(APPS, { state: "running", type: "nextjs", q: "pic" }).map((a) => a.domain)).toEqual(["picconia.com"]);
  });

  it("keeps everything without filters", () => {
    expect(filterApps(APPS, {})).toHaveLength(APPS.length);
    expect(isFiltered({})).toBe(false);
    expect(isFiltered({ type: "static" })).toBe(true);
  });
});

describe("appTypes", () => {
  it("lists each type once, alphabetically, skipping unknown ones", () => {
    expect(appTypes(APPS)).toEqual(["nextjs", "python", "static"]);
  });
});

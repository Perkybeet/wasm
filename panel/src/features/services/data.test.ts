import { describe, expect, it } from "vitest";

import type { ServiceInfo } from "./data";
import { filterServices, isFiltered, serviceState, validateServicesSearch } from "./data";

function service(name: string, active: boolean, description: string | null = null): ServiceInfo {
  return { name, active, enabled: true, status: active ? "running" : "stopped", description, pid: null, uptime: null, memory: null, managed: true };
}

describe("serviceState", () => {
  it("is running when active, stopped otherwise", () => {
    expect(serviceState({ active: true })).toBe("running");
    expect(serviceState({ active: false })).toBe("stopped");
  });
});

describe("validateServicesSearch", () => {
  it("keeps a trimmed query", () => {
    expect(validateServicesSearch({ q: " worker " })).toEqual({ q: "worker" });
  });

  it("drops what is malformed", () => {
    expect(validateServicesSearch({ q: "" })).toEqual({});
    expect(validateServicesSearch({ q: 42 })).toEqual({});
    expect(validateServicesSearch({})).toEqual({});
  });

  it("bounds free text", () => {
    expect(validateServicesSearch({ q: "x".repeat(500) }).q).toHaveLength(200);
  });
});

describe("filterServices", () => {
  const services = [service("wasm-queue", true, "node worker.js"), service("wasm-cache", false, "redis-server"), service("nginx", true)];

  it("matches the name or the command, case-insensitively", () => {
    expect(filterServices(services, { q: "QUEUE" }).map((s) => s.name)).toEqual(["wasm-queue"]);
    expect(filterServices(services, { q: "redis" }).map((s) => s.name)).toEqual(["wasm-cache"]);
  });

  it("keeps everything without a filter", () => {
    expect(filterServices(services, {})).toHaveLength(services.length);
    expect(isFiltered({})).toBe(false);
    expect(isFiltered({ q: "x" })).toBe(true);
  });

  it("does not fail a service with no recorded command", () => {
    expect(filterServices(services, { q: "nginx" }).map((s) => s.name)).toEqual(["nginx"]);
  });
});

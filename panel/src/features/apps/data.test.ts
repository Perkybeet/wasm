import { describe, expect, it } from "vitest";

import type { AppInfo, Deployment } from "./data";
import { appLimits, appReading, deployMoment, latestDeployByDomain, readsApps } from "./data";

function deploy(id: number, domain: string, status = "success"): Deployment {
  return { id, domain, status, triggered_by: "cli", has_log: true, started_at: "2026-09-25T10:00:00", finished_at: null };
}

describe("latestDeployByDomain", () => {
  it("keeps the newest deploy of each domain, whatever the order", () => {
    const latest = latestDeployByDomain([deploy(3, "a.com"), deploy(9, "b.com"), deploy(7, "a.com"), deploy(1, "b.com")]);
    expect(latest.get("a.com")?.id).toBe(7);
    expect(latest.get("b.com")?.id).toBe(9);
    expect(latest.size).toBe(2);
  });
});

describe("deployMoment", () => {
  it("is the end of a finished deploy and the start of a running one", () => {
    expect(deployMoment({ ...deploy(1, "a.com"), finished_at: "2026-09-25T10:05:00" })).toBe("2026-09-25T10:05:00");
    expect(deployMoment(deploy(1, "a.com"))).toBe("2026-09-25T10:00:00");
  });
});

describe("appReading", () => {
  const snapshot = { "cpu.percent": 12, "app.shop.com.cpu.percent": 3.5, "app.shop.com.mem.bytes": 100_663_296 };

  it("reads the app's own cgroup metrics", () => {
    expect(appReading(snapshot, "shop.com")).toEqual({ cpu: 3.5, memory: 100_663_296 });
  });

  it("says there is no reading rather than zero", () => {
    expect(appReading(snapshot, "other.com")).toEqual({ cpu: null, memory: null });
    expect(appReading(undefined, "shop.com")).toEqual({ cpu: null, memory: null });
  });

  it("knows whether the collector reads apps at all", () => {
    expect(readsApps(snapshot)).toBe(true);
    expect(readsApps({ "cpu.percent": 12 })).toBe(false);
    expect(readsApps(undefined)).toBe(false);
  });
});

describe("appLimits", () => {
  const base: AppInfo = { domain: "a.com", name: "a.com", status: "running", active: true, enabled: true, layout: "releases", webhook_enabled: false };

  it("reads the unit's limits in bytes and percent", () => {
    expect(appLimits({ ...base, memory_max_mb: 512, cpu_quota_percent: 50, tasks_max: 256 })).toEqual({
      memory: 512 * 1024 * 1024,
      cpu: 50,
      tasks: 256,
    });
  });

  it("treats absent or zero limits as no limit", () => {
    expect(appLimits({ ...base, memory_max_mb: null, cpu_quota_percent: 0 })).toEqual({ memory: null, cpu: null, tasks: null });
  });
});

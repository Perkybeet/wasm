import { describe, expect, it } from "vitest";

import { changed, directives, draftOf, parseLimits } from "./limits";

const EMPTY = { memory: "", cpu: "", tasks: "" };

describe("parseLimits", () => {
  it("reads empty fields as no limit", () => {
    expect(parseLimits(EMPTY, 4)).toEqual({
      values: { memory_max_mb: null, cpu_quota_percent: null, tasks_max: null },
      errors: {},
    });
  });

  it("reads whole numbers, spaces around them allowed", () => {
    expect(parseLimits({ memory: " 512 ", cpu: "150", tasks: "256" }, 4).values).toEqual({
      memory_max_mb: 512,
      cpu_quota_percent: 150,
      tasks_max: 256,
    });
  });

  it("refuses what is not a whole number", () => {
    const { errors } = parseLimits({ memory: "512M", cpu: "1.5", tasks: "-3" }, 4);
    expect(errors.memory).toBe("Enter a whole number, or leave it empty for no limit.");
    expect(errors.cpu).toBe("Enter a whole number, or leave it empty for no limit.");
    expect(errors.tasks).toBe("Enter a whole number, or leave it empty for no limit.");
  });

  it("holds the backend's bounds, in its words", () => {
    expect(parseLimits({ ...EMPTY, memory: "63" }, 4).errors.memory).toBe(
      "A memory limit of 63M is too small. Allow at least 64M, or no limit.",
    );
    expect(parseLimits({ ...EMPTY, memory: "64" }, 4).errors).toEqual({});
    expect(parseLimits({ ...EMPTY, tasks: "15" }, 4).errors.tasks).toBe("A limit of 15 tasks is too small. Allow at least 16, or no limit.");
    expect(parseLimits({ ...EMPTY, tasks: "16" }, 4).errors).toEqual({});
  });

  it("bounds the CPU quota by the machine's CPUs when they are known", () => {
    expect(parseLimits({ ...EMPTY, cpu: "400" }, 4).errors).toEqual({});
    expect(parseLimits({ ...EMPTY, cpu: "401" }, 4).errors.cpu).toBe(
      "A CPU quota of 401% is not possible here. This machine has 4 CPUs: use 1% to 400%.",
    );
    expect(parseLimits({ ...EMPTY, cpu: "150" }, 1).errors.cpu).toBe(
      "A CPU quota of 150% is not possible here. This machine has 1 CPU: use 1% to 100%.",
    );
    expect(parseLimits({ ...EMPTY, cpu: "0" }, 4).errors.cpu).toMatch(/^A CPU quota of 0% is not possible here/);
  });

  it("only refuses a quota below 1% when the CPUs are unknown", () => {
    expect(parseLimits({ ...EMPTY, cpu: "3200" }, null).errors).toEqual({});
    expect(parseLimits({ ...EMPTY, cpu: "0" }, null).errors.cpu).toBe("A CPU quota of 0% is not possible. Use at least 1%.");
  });
});

describe("the form's text", () => {
  it("starts from the app's limits, empty where it has none", () => {
    expect(draftOf({ memory_max_mb: 512, cpu_quota_percent: null, tasks_max: 256 })).toEqual({ memory: "512", cpu: "", tasks: "256" });
  });

  it("knows when it says something new", () => {
    const current = draftOf({ memory_max_mb: 512 });
    expect(changed({ ...current }, current)).toBe(false);
    expect(changed({ ...current, memory: " 512 " }, current)).toBe(false);
    expect(changed({ ...current, cpu: "50" }, current)).toBe(true);
  });

  it("writes the limits as the unit's directives", () => {
    expect(directives({ memory_max_mb: 512, cpu_quota_percent: 150, tasks_max: 256 })).toEqual([
      "MemoryMax=512M",
      "CPUQuota=150%",
      "TasksMax=256",
    ]);
    expect(directives({})).toEqual([]);
  });
});

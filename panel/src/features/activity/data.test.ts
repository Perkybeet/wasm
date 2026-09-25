import { describe, expect, it } from "vitest";

import type { ActivityJob } from "./data";
import { actionLabel, filterJobs, isFiltered, jobResource, validateActivitySearch } from "./data";

function job(overrides: Partial<ActivityJob> = {}): ActivityJob {
  return {
    id: "abc123",
    type: "update",
    name: "Update example.com",
    description: "Updating the application at example.com",
    status: "completed",
    progress: 100,
    total_steps: 100,
    current_step: "",
    created_at: "2026-09-25T10:00:00Z",
    started_at: "2026-09-25T10:00:01Z",
    completed_at: "2026-09-25T10:00:30Z",
    result: null,
    error: null,
    logs: [],
    metadata: {},
    ...overrides,
  };
}

describe("actionLabel", () => {
  it("translates every JobType to a sentence-case word", () => {
    expect(actionLabel("deploy")).toBe("Deploy");
    expect(actionLabel("cert_renew")).toBe("Renew certificate");
    expect(actionLabel("service_action")).toBe("Service action");
  });

  it("shows an unrecognised type verbatim rather than guessing", () => {
    expect(actionLabel("mystery")).toBe("mystery");
  });
});

describe("jobResource", () => {
  it("reads the domain out of metadata", () => {
    expect(jobResource(job({ metadata: { domain: "example.com" } }))).toBe("example.com");
  });

  it("is null for a job that named no domain", () => {
    expect(jobResource(job({ metadata: {} }))).toBeNull();
  });
});

describe("validateActivitySearch", () => {
  it("keeps a known status and type, and any domain", () => {
    expect(validateActivitySearch({ status: "failed", type: "deploy", domain: "example.com" })).toEqual({
      status: "failed",
      type: "deploy",
      domain: "example.com",
    });
  });

  it("drops an unknown status or type instead of failing", () => {
    expect(validateActivitySearch({ status: "exploded", type: "not-a-type" })).toEqual({});
  });
});

describe("filterJobs", () => {
  const jobs = [job({ id: "1", type: "deploy" }), job({ id: "2", type: "backup" })];

  it("keeps only the matching type - status and domain are filtered server-side", () => {
    expect(filterJobs(jobs, { type: "backup" }).map((j) => j.id)).toEqual(["2"]);
  });

  it("keeps everything without a type filter", () => {
    expect(filterJobs(jobs, {})).toHaveLength(2);
    expect(isFiltered({})).toBe(false);
    expect(isFiltered({ domain: "example.com" })).toBe(true);
  });
});

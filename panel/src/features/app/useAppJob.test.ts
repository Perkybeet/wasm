import { describe, expect, it } from "vitest";

import type { Job } from "../../api/queries/jobs";
import { jobStep, jobWords } from "./useAppJob";

const JOB: Job = {
  id: "96bad296",
  type: "update",
  name: "Update picconia.com",
  description: "Updating the application at picconia.com",
  status: "running",
  progress: 0,
  total_steps: 100,
  current_step: "",
  created_at: "2026-09-25T19:21:13",
  logs: [{ timestamp: "2026-09-25T19:21:13", level: "info", message: "Installing dependencies", step: 1 }],
  metadata: { domain: "picconia.com" },
};

describe("jobWords", () => {
  it("names each job the way the header says it", () => {
    expect(jobWords("update")).toEqual({ running: "Updating", noun: "Update" });
    expect(jobWords("restore")).toEqual({ running: "Rolling back", noun: "Rollback" });
    expect(jobWords("migrate")).toEqual({ running: "Migrating", noun: "Migration" });
    expect(jobWords("something_new")).toEqual({ running: "Working", noun: "Job" });
  });
});

describe("jobStep", () => {
  it("is the newest line the job logged", () => {
    expect(jobStep(JOB)).toBe("Installing dependencies");
  });

  it("is nothing when the job has not logged", () => {
    expect(jobStep({ ...JOB, logs: [] })).toBeNull();
    expect(jobStep({ ...JOB, logs: [{ message: "  " }] })).toBeNull();
  });
});

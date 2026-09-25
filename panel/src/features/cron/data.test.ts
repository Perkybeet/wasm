import { describe, expect, it } from "vitest";

import type { CronJob } from "./data";
import { filterJobs, isFiltered, runStatus, validateCronSearch } from "./data";

function job(name: string, command: string): CronJob {
  return {
    name,
    command,
    user: "wasm",
    working_directory: "/var/www",
    app_domain: "",
    schedule: "daily",
    on_calendar: "*-*-* 02:00:00",
    enabled: true,
    next_run: "Fri 2026-09-26 02:00:00 UTC",
    last_run: "never",
    last_exit_code: null,
    last_result: "never ran",
  };
}

describe("runStatus", () => {
  it("reads a successful run as running/Succeeded", () => {
    expect(runStatus("success")).toEqual({ state: "running", label: "Succeeded" });
  });

  it("reads a job that never ran as unknown", () => {
    expect(runStatus("never ran")).toEqual({ state: "unknown", label: "Never run" });
    expect(runStatus(null)).toEqual({ state: "unknown", label: "Never run" });
    expect(runStatus(undefined)).toEqual({ state: "unknown", label: "Never run" });
  });

  it("reads any other systemd Result as failed, keeping the word", () => {
    expect(runStatus("exit-code")).toEqual({ state: "failed", label: "exit-code" });
    expect(runStatus("timeout")).toEqual({ state: "failed", label: "timeout" });
    expect(runStatus("signal")).toEqual({ state: "failed", label: "signal" });
  });
});

describe("validateCronSearch", () => {
  it("keeps a trimmed query and drops malformed input", () => {
    expect(validateCronSearch({ q: " nightly " })).toEqual({ q: "nightly" });
    expect(validateCronSearch({ q: "" })).toEqual({});
    expect(validateCronSearch({ q: 1 })).toEqual({});
  });
});

describe("filterJobs", () => {
  const jobs = [job("nightly-backup", "wasm backup create example.com"), job("hourly-sync", "rsync -a /a /b")];

  it("matches the name or the command", () => {
    expect(filterJobs(jobs, { q: "BACKUP" }).map((j) => j.name)).toEqual(["nightly-backup"]);
    expect(filterJobs(jobs, { q: "rsync" }).map((j) => j.name)).toEqual(["hourly-sync"]);
  });

  it("keeps everything without a filter", () => {
    expect(filterJobs(jobs, {})).toHaveLength(2);
    expect(isFiltered({})).toBe(false);
    expect(isFiltered({ q: "x" })).toBe(true);
  });
});

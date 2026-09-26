import { describe, expect, it } from "vitest";

import type { AuditEntry, ActivityJob } from "./data";
import {
  actionWords,
  actorWords,
  auditActionLabel,
  auditResultStatus,
  describeActor,
  isFiltered,
  jobActionLabel,
  mergeActivity,
  resourceOf,
  resultOptions,
  resultValidFor,
  rowActor,
  validateActivitySearch,
} from "./data";

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
    actor: null,
    ...overrides,
  };
}

/** The one row a single-row merge produced, asserting there is exactly one rather than assuming it. */
function only<T>(items: readonly T[]): T {
  expect(items).toHaveLength(1);
  const [item] = items;
  return item as T;
}

function entry(overrides: Partial<AuditEntry> = {}): AuditEntry {
  return {
    timestamp: "2026-09-25T10:00:00+00:00",
    action: "auth.login",
    result: "success",
    actor: "a1b2c3d4e5f6",
    client_ip: "203.0.113.1",
    resource: "/api/auth/login",
    detail: null,
    ...overrides,
  };
}

describe("jobActionLabel", () => {
  it("translates every JobType to a sentence-case word", () => {
    expect(jobActionLabel("deploy")).toBe("Deploy");
    expect(jobActionLabel("cert_renew")).toBe("Renew certificate");
  });

  it("shows an unrecognised type verbatim rather than guessing", () => {
    expect(jobActionLabel("mystery")).toBe("mystery");
  });
});

describe("auditActionLabel", () => {
  it("translates a known action", () => {
    expect(auditActionLabel("auth.login")).toBe("Sign-in attempt");
    expect(auditActionLabel("auth.scope")).toBe("Failed a scope check");
  });

  it("words the security middleware's generic per-request entry by its method", () => {
    expect(auditActionLabel("api.post")).toBe("POST request");
    expect(auditActionLabel("api.delete")).toBe("DELETE request");
  });

  it("shows an unrecognised action verbatim rather than guessing", () => {
    expect(auditActionLabel("something.new")).toBe("something.new");
  });
});

describe("auditResultStatus", () => {
  it("maps every result the backend writes", () => {
    expect(auditResultStatus("success").state).toBe("running");
    expect(auditResultStatus("denied").state).toBe("failed");
    expect(auditResultStatus("denied").attention).toBe(true);
    expect(auditResultStatus("locked").label).toBe("Locked out");
  });

  it("humanises an unrecognised result instead of guessing its state", () => {
    const view = auditResultStatus("weird_thing");
    expect(view.state).toBe("unknown");
    expect(view.label).toBe("Weird thing");
  });

  it("treats the security middleware's generic error:<status> as a failure", () => {
    const view = auditResultStatus("error:401");
    expect(view.state).toBe("failed");
    expect(view.label).toBe("Error 401");
    expect(view.attention).toBe(true);
  });
});

describe("describeActor", () => {
  it("names the master token", () => {
    expect(describeActor("master")).toEqual({ label: "The master token", raw: "master" });
  });

  it("names an API token by its own name, keeping the raw value", () => {
    expect(describeActor("token:ci-deploy")).toEqual({ label: 'Token "ci-deploy"', raw: "token:ci-deploy" });
  });

  it("names a webhook delivery", () => {
    expect(describeActor("webhook").label).toBe("A webhook delivery");
  });

  it("names an anonymous, unauthenticated attempt", () => {
    expect(describeActor("anonymous").label).toBe("Anonymous");
  });

  it("names a browser session by a short id, keeping the full raw value", () => {
    const words = describeActor("a1b2c3d4e5f6g7h8");
    expect(words.label).toBe("Session a1b2c3d4");
    expect(words.raw).toBe("a1b2c3d4e5f6g7h8");
  });
});

describe("actorWords / rowActor", () => {
  it("reads a job row's actor", () => {
    const row = only(mergeActivity({ jobs: [job({ actor: "master" })], jobsComplete: true, entries: [], auditComplete: true }).rows);
    expect(rowActor(row)).toBe("master");
    expect(actorWords(row).label).toBe("The master token");
  });

  it("says a job with no recorded actor is not recorded, rather than guessing", () => {
    const row = only(mergeActivity({ jobs: [job({ actor: null })], jobsComplete: true, entries: [], auditComplete: true }).rows);
    expect(actorWords(row)).toEqual({ label: "Not recorded", raw: "-" });
  });

  it("reads an audit row's actor", () => {
    const row = only(mergeActivity({ jobs: [], jobsComplete: true, entries: [entry({ actor: "token:ci" })], auditComplete: true }).rows);
    expect(rowActor(row)).toBe("token:ci");
  });
});

describe("actionWords / resourceOf", () => {
  it("words a job row from its type and domain", () => {
    const row = only(
      mergeActivity({
        jobs: [job({ type: "deploy", metadata: { domain: "shop.example.com" } })],
        jobsComplete: true,
        entries: [],
        auditComplete: true,
      }).rows,
    );
    expect(actionWords(row)).toEqual({ label: "Deploy", raw: "deploy" });
    expect(resourceOf(row)).toBe("shop.example.com");
  });

  it("words an audit row from its action and resource", () => {
    const row = only(
      mergeActivity({
        jobs: [],
        jobsComplete: true,
        entries: [entry({ action: "auth.scope", resource: "/api/apps/shop.example.com" })],
        auditComplete: true,
      }).rows,
    );
    expect(actionWords(row)).toEqual({ label: "Failed a scope check", raw: "auth.scope" });
    expect(resourceOf(row)).toBe("/api/apps/shop.example.com");
  });
});

describe("mergeActivity", () => {
  it("merges both sources into one newest-first timeline", () => {
    const jobs = [job({ id: "j1", started_at: "2026-09-25T09:00:00Z" })];
    const entries = [entry({ timestamp: "2026-09-25T09:30:00+00:00" }), entry({ timestamp: "2026-09-25T08:30:00+00:00" })];
    const { rows } = mergeActivity({ jobs, jobsComplete: true, entries, auditComplete: true });
    expect(rows.map((row) => row.timestamp)).toEqual([
      "2026-09-25T09:30:00+00:00",
      "2026-09-25T09:00:00Z",
      "2026-09-25T08:30:00+00:00",
    ]);
  });

  it("reports no more pages once both sources are complete", () => {
    const { hasMore } = mergeActivity({ jobs: [job()], jobsComplete: true, entries: [entry()], auditComplete: true });
    expect(hasMore).toBe(false);
  });

  it("filters the merged rows by actor without affecting which raw rows are trusted", () => {
    const jobs = [job({ id: "j1", actor: "master", started_at: "2026-09-25T09:00:00Z" })];
    const entries = [entry({ actor: "token:ci", timestamp: "2026-09-25T09:30:00+00:00" })];
    const { rows } = mergeActivity({ jobs, jobsComplete: true, entries, auditComplete: true, actor: "master" });
    expect(only(rows).kind).toBe("job");
  });

  it("holds back rows older than the shallower source's cutoff, so a later page never reorders what is already shown", () => {
    // Audit's fetched page reaches back only to 10:00 (more of it may exist, unfetched); jobs
    // already reached all the way to 08:00. A job at 09:00 sits in the gap: audit might yet
    // turn out to have an entry between 09:00 and 10:00, which would have to sort above it -
    // so it must not be shown until "Load more" extends audit's coverage past it.
    const jobs = [
      job({ id: "recent", started_at: "2026-09-25T11:00:00Z" }),
      job({ id: "in-the-gap", started_at: "2026-09-25T09:00:00Z" }),
      job({ id: "oldest", started_at: "2026-09-25T08:00:00Z" }),
    ];
    const entries = [entry({ timestamp: "2026-09-25T10:30:00+00:00" }), entry({ timestamp: "2026-09-25T10:00:00+00:00" })];

    const { rows, hasMore } = mergeActivity({ jobs, jobsComplete: true, entries, auditComplete: false });

    expect(hasMore).toBe(true);
    const ids = rows.filter((row) => row.kind === "job").map((row) => row.job.id);
    expect(ids).toEqual(["recent"]);
    expect(rows.some((row) => row.kind === "job" && row.job.id === "in-the-gap")).toBe(false);
  });

  it("shows everything once the previously incomplete source catches up past the gap", () => {
    const jobs = [
      job({ id: "recent", started_at: "2026-09-25T11:00:00Z" }),
      job({ id: "in-the-gap", started_at: "2026-09-25T09:00:00Z" }),
    ];
    // A second page of audit now reaches back to 08:30, past the job in the gap.
    const entries = [
      entry({ timestamp: "2026-09-25T10:30:00+00:00" }),
      entry({ timestamp: "2026-09-25T10:00:00+00:00" }),
      entry({ timestamp: "2026-09-25T08:30:00+00:00" }),
    ];

    const { rows, hasMore } = mergeActivity({ jobs, jobsComplete: true, entries, auditComplete: true });

    expect(hasMore).toBe(false);
    expect(rows).toHaveLength(5);
    expect(rows.some((row) => row.kind === "job" && row.job.id === "in-the-gap")).toBe(true);
  });
});

describe("resultValidFor / resultOptions", () => {
  it("accepts a job status only under jobs or no kind", () => {
    expect(resultValidFor("failed", "jobs")).toBe(true);
    expect(resultValidFor("failed", undefined)).toBe(true);
    expect(resultValidFor("failed", "audit")).toBe(false);
  });

  it("accepts an audit result only under audit or no kind", () => {
    expect(resultValidFor("denied", "audit")).toBe(true);
    expect(resultValidFor("denied", undefined)).toBe(true);
    expect(resultValidFor("denied", "jobs")).toBe(false);
  });

  it("offers only the relevant vocabulary once kind narrows it", () => {
    expect(resultOptions("jobs").map((o) => o.value)).not.toContain("denied");
    expect(resultOptions("audit").map((o) => o.value)).not.toContain("failed");
  });

  it("prefixes both vocabularies when everything is shown, so the same word is not offered twice unlabelled", () => {
    const options = resultOptions(undefined);
    expect(options.find((o) => o.value === "failed")?.label).toBe("Job: Failed");
    expect(options.find((o) => o.value === "failure")?.label).toBe("Action: Failed");
  });
});

describe("validateActivitySearch", () => {
  it("keeps a known kind, a result valid for it, and any actor", () => {
    expect(validateActivitySearch({ kind: "jobs", result: "failed", actor: "master" })).toEqual({
      kind: "jobs",
      result: "failed",
      actor: "master",
    });
  });

  it("drops a result that does not apply to the given kind, instead of failing", () => {
    expect(validateActivitySearch({ kind: "jobs", result: "denied" })).toEqual({ kind: "jobs" });
  });

  it("drops an unknown kind", () => {
    expect(validateActivitySearch({ kind: "everything" })).toEqual({});
  });
});

describe("isFiltered", () => {
  it("is false with nothing set and true with any filter", () => {
    expect(isFiltered({})).toBe(false);
    expect(isFiltered({ actor: "master" })).toBe(true);
    expect(isFiltered({ kind: "audit" })).toBe(true);
  });
});

describe("withoutRequestEchoes", () => {
  const entry = (action: string, timestamp: string, resource = "/api/auth/login"): AuditEntry => ({
    action,
    timestamp,
    resource,
    actor: "anonymous",
    result: "ok",
  });
  const rows = (entries: AuditEntry[]) => mergeActivity({ jobs: [], jobsComplete: true, entries, auditComplete: true }).rows;

  it("drops a request's generic record when the endpoint recorded the same moment itself", () => {
    const merged = rows([
      entry("api.post", "2026-09-25T19:00:01+00:00"),
      entry("auth.login", "2026-09-25T19:00:01+00:00"),
    ]);
    expect(merged.map((row) => (row.kind === "audit" ? row.entry.action : row.kind))).toEqual(["auth.login"]);
  });

  it("keeps the generic record of a request nothing else described", () => {
    const merged = rows([
      entry("api.post", "2026-09-25T19:00:01+00:00", "/api/apps/shop.example.com/restart"),
      entry("auth.login", "2026-09-25T19:00:01+00:00"),
      entry("api.post", "2026-09-25T19:05:00+00:00"),
    ]);
    expect(merged.map((row) => (row.kind === "audit" ? `${row.entry.action} ${row.entry.resource ?? ""}` : row.kind))).toEqual([
      "api.post /api/auth/login",
      "api.post /api/apps/shop.example.com/restart",
      "auth.login /api/auth/login",
    ]);
  });
});

import { describe, expect, it } from "vitest";

import { applyDraft, describeCounts, diffEnv, draftRows, isMasked, summarise } from "./draft";
import type { DraftOp } from "./draft";

const MASKED = new Map([
  ["NODE_ENV", "production"],
  ["DATABASE_URL", "postgres://app:***@db/app"],
  ["SESSION_SECRET", "***"],
]);

const CLEAR = new Map([
  ["NODE_ENV", "production"],
  ["DATABASE_URL", "postgres://app:hunter2@db/app"],
  ["SESSION_SECRET", "s3cr3t"],
]);

describe("applyDraft", () => {
  it("replays sets, removals and restores in order", () => {
    const ops: DraftOp[] = [
      { kind: "set", name: "PORT", value: "3000" },
      { kind: "remove", name: "NODE_ENV" },
      { kind: "set", name: "SESSION_SECRET", value: "new" },
      { kind: "restore", name: "SESSION_SECRET" },
    ];
    expect(Object.fromEntries(applyDraft(CLEAR, ops))).toEqual({
      DATABASE_URL: "postgres://app:hunter2@db/app",
      SESSION_SECRET: "s3cr3t",
      PORT: "3000",
    });
  });

  it("builds the saved map from the values in clear, never from the placeholders", () => {
    const ops: DraftOp[] = [{ kind: "set", name: "NODE_ENV", value: "staging" }];
    expect(applyDraft(CLEAR, ops).get("SESSION_SECRET")).toBe("s3cr3t");
  });

  it("replaces everything with a pasted file, and a restore brings one back", () => {
    const ops: DraftOp[] = [
      { kind: "replace", variables: new Map([["PORT", "8080"]]) },
      { kind: "restore", name: "SESSION_SECRET" },
    ];
    expect(Object.fromEntries(applyDraft(CLEAR, ops))).toEqual({ PORT: "8080", SESSION_SECRET: "s3cr3t" });
  });
});

describe("draftRows", () => {
  it("keeps the file's order, marks what the draft does, and lists the added ones last", () => {
    const rows = draftRows(MASKED, [
      { kind: "set", name: "API_URL", value: "https://api" },
      { kind: "remove", name: "NODE_ENV" },
      { kind: "set", name: "SESSION_SECRET", value: "rotated" },
    ]);
    expect(rows.map((row) => [row.name, row.state])).toEqual([
      ["NODE_ENV", "removed"],
      ["DATABASE_URL", "unchanged"],
      ["SESSION_SECRET", "changed"],
      ["API_URL", "added"],
    ]);
    expect(summarise(rows)).toEqual({ added: 1, changed: 1, removed: 1, total: 3 });
  });

  it("knows a secret set to its own value is unchanged once the values were read in clear", () => {
    const ops: DraftOp[] = [{ kind: "set", name: "SESSION_SECRET", value: "s3cr3t" }];
    expect(draftRows(MASKED, ops).find((row) => row.name === "SESSION_SECRET")?.state).toBe("changed");
    expect(draftRows(MASKED, ops, CLEAR).find((row) => row.name === "SESSION_SECRET")?.state).toBe("unchanged");
  });

  it("has nothing to say for an undone change", () => {
    const rows = draftRows(MASKED, [
      { kind: "set", name: "NODE_ENV", value: "dev" },
      { kind: "restore", name: "NODE_ENV" },
    ]);
    expect(summarise(rows).total).toBe(0);
  });
});

describe("diffEnv", () => {
  it("compares the values the file really holds", () => {
    const next = applyDraft(CLEAR, [
      { kind: "set", name: "SESSION_SECRET", value: "s3cr3t" },
      { kind: "set", name: "PORT", value: "3000" },
      { kind: "remove", name: "NODE_ENV" },
    ]);
    const diff = diffEnv(CLEAR, next);
    expect(diff.changes).toEqual([
      { name: "PORT", kind: "added", before: null, after: "3000" },
      { name: "NODE_ENV", kind: "removed", before: "production", after: null },
    ]);
    expect(diff.unchanged).toBe(2);
  });

  it("keeps a value with surrounding spaces as it is: the writer quotes what needs it", () => {
    const diff = diffEnv(new Map(), new Map([["GREETING", "  hola  "]]));
    expect(diff.changes[0]).toEqual({ name: "GREETING", kind: "added", before: null, after: "  hola  " });
  });
});

describe("helpers", () => {
  it("tells a masked value from a plain one", () => {
    expect(isMasked("***")).toBe(true);
    expect(isMasked("postgres://u:***@h/db")).toBe(true);
    expect(isMasked("production")).toBe(false);
  });

  it("says the counts without the zeros", () => {
    expect(describeCounts({ added: 2, changed: 0, removed: 1 })).toBe("2 added, 1 removed");
  });
});

/**
 * Unsaved changes to an app's environment, kept as the operations the operator made rather
 * than as a copy of the file.
 *
 * The page only ever holds the masked variables (secrets come back as `***`), and saving
 * replaces the whole file, so a copy edited in the page would write the placeholders back.
 * Operations are replayed instead: over the masked map to draw the table, and over the
 * unmasked map, read fresh when the operator reviews, to build exactly what is saved.
 */

import { readBack } from "./dotenv";

export type DraftOp =
  | { kind: "set"; name: string; value: string }
  | { kind: "remove"; name: string }
  /** Put back the value the file holds now, undoing earlier operations on the name. */
  | { kind: "restore"; name: string }
  /** Everything not in `variables` goes; everything in it is set. */
  | { kind: "replace"; variables: ReadonlyMap<string, string> };

export type EnvMap = ReadonlyMap<string, string>;

/** The map the operations turn `base` into. */
export function applyDraft(base: EnvMap, ops: readonly DraftOp[]): Map<string, string> {
  const next = new Map(base);
  for (const op of ops) {
    switch (op.kind) {
      case "set":
        next.set(op.name, op.value);
        break;
      case "remove":
        next.delete(op.name);
        break;
      case "restore": {
        const value = base.get(op.name);
        if (value === undefined) next.delete(op.name);
        else next.set(op.name, value);
        break;
      }
      case "replace":
        next.clear();
        for (const [name, value] of op.variables) next.set(name, value);
        break;
    }
  }
  return next;
}

export type RowState = "unchanged" | "added" | "changed" | "removed";

export interface EnvRow {
  name: string;
  state: RowState;
  /** What the file holds now, as the page knows it (masked for a secret); null when added. */
  current: string | null;
  /** What the draft sets; null when removed or untouched. */
  draft: string | null;
}

/**
 * The table's rows: every variable of the file in its order, then the added ones, each with
 * what the draft does to it. A secret is compared in clear when the page has read the values
 * in clear (`clear`); until then a draft that sets it reads as changed even when the operator
 * typed the same value, and the review, over the values in clear, has the final word.
 */
export function draftRows(masked: EnvMap, ops: readonly DraftOp[], clear: EnvMap | null = null): EnvRow[] {
  const next = applyDraft(masked, ops);
  const rows: EnvRow[] = [];
  for (const [name, current] of masked) {
    const value = next.get(name);
    const known = clear?.get(name) ?? current;
    if (value === undefined) rows.push({ name, state: "removed", current, draft: null });
    else if (value !== current && value !== known) rows.push({ name, state: "changed", current, draft: value });
    else rows.push({ name, state: "unchanged", current, draft: null });
  }
  for (const [name, value] of next) {
    if (!masked.has(name)) rows.push({ name, state: "added", current: null, draft: value });
  }
  return rows;
}

export interface EnvChange {
  name: string;
  kind: "added" | "changed" | "removed";
  before: string | null;
  after: string | null;
  /** What WASM will read back when it differs from `after` (the writer does not quote). */
  readsBackAs: string | null;
}

export interface EnvDiff {
  changes: EnvChange[];
  unchanged: number;
  /** The complete map to save. */
  next: Map<string, string>;
}

/** What saving would change, over the values the file really holds. */
export function diffEnv(before: EnvMap, next: Map<string, string>): EnvDiff {
  const changes: EnvChange[] = [];
  let unchanged = 0;
  const withReadBack = (value: string): string | null => {
    const back = readBack(value);
    return back === value ? null : back;
  };
  for (const [name, value] of next) {
    const old = before.get(name);
    if (old === undefined) changes.push({ name, kind: "added", before: null, after: value, readsBackAs: withReadBack(value) });
    else if (old !== value) changes.push({ name, kind: "changed", before: old, after: value, readsBackAs: withReadBack(value) });
    else unchanged += 1;
  }
  for (const [name, value] of before) {
    if (!next.has(name)) changes.push({ name, kind: "removed", before: value, after: null, readsBackAs: null });
  }
  return { changes, unchanged, next };
}

/** Counts of a draft's rows by what happens to them. */
export function summarise(rows: readonly EnvRow[]): { added: number; changed: number; removed: number; total: number } {
  const added = rows.filter((row) => row.state === "added").length;
  const changed = rows.filter((row) => row.state === "changed").length;
  const removed = rows.filter((row) => row.state === "removed").length;
  return { added, changed, removed, total: added + changed + removed };
}

/** The API masks secrets with this fixed placeholder, and passwords inside URLs with it too. */
export const REDACTED = "***";

/** True when the masked value hides something, so revealing it needs the unmasked read. */
export function isMasked(value: string): boolean {
  return value.includes(REDACTED);
}

/** "1 added, 2 changed" from counts, leaving out the zeros. */
export function describeCounts(counts: { added: number; changed: number; removed: number }): string {
  const parts = [
    counts.added > 0 ? `${String(counts.added)} added` : null,
    counts.changed > 0 ? `${String(counts.changed)} changed` : null,
    counts.removed > 0 ? `${String(counts.removed)} removed` : null,
  ].filter((part): part is string => part !== null);
  return parts.join(", ");
}

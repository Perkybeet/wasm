/**
 * The health check and retention forms: what each field accepts and how its value reaches
 * `PATCH /api/apps/{domain}/health` and `PATCH /api/apps/{domain}/releases/retention`.
 *
 * The rules and the words mirror `wasm.validators.health` and the store's retention bounds,
 * which are the checks that count; these only save a round trip. An empty field is the
 * default, exactly as a null is in the request.
 */

import type { App } from "../../../api/queries/apps";

/** The gate's own defaults, as `docs/releases.md` states them. */
export const HEALTH_DEFAULTS = { path: "/", expect: "any status below 500", timeout: 30 } as const;

export const HEALTH_TIMEOUT_MIN = 5;
export const HEALTH_TIMEOUT_MAX = 600;
const PATH_MAX = 1024;

export const RETENTION_MIN = 1;
export const RETENTION_MAX = 50;

export interface HealthDraft {
  path: string;
  expect: string;
  timeout: string;
}

export interface HealthValues {
  path: string | null;
  expect: string | null;
  timeout: number | null;
}

export type HealthErrors = Partial<Record<keyof HealthDraft, string>>;

/** The form as the app has it: an unset setting is an empty field. */
export function healthDraftOf(app: Pick<App, "health_path" | "health_expect" | "health_timeout">): HealthDraft {
  return {
    path: app.health_path ?? "",
    expect: app.health_expect ?? "",
    timeout: unset(app.health_timeout) ? "" : String(app.health_timeout),
  };
}

export function sameHealth(a: HealthDraft, b: HealthDraft): boolean {
  return a.path.trim() === b.path.trim() && a.expect.trim() === b.expect.trim() && a.timeout.trim() === b.timeout.trim();
}

const PATH_HOW = "Give a path on the application, such as /healthz or /api/health?deep=1.";
const EXPECT_HOW = "Use statuses and ranges from 100 to 599, separated by commas: 200-399, or 200,204, or 200-299,301.";
const ITEM = /^(\d{3})(?:-(\d{3}))?$/;

function pathProblem(path: string): string | null {
  if (!path.startsWith("/") || path.startsWith("//")) {
    return `${PATH_HOW} A scheme or a host is not accepted: the check always asks the application itself, on 127.0.0.1.`;
  }
  if (path.length > PATH_MAX) return `The path is longer than ${String(PATH_MAX)} characters.`;
  // Printable ASCII only, as the gate's request line needs: no space, no control character.
  if (!/^[\x21-\x7e]+$/.test(path)) return `${PATH_HOW} Percent-encode anything else (%20 for a space).`;
  return null;
}

function expectProblem(expect: string): string | null {
  for (const raw of expect.split(",")) {
    const match = ITEM.exec(raw.trim());
    if (match === null) return EXPECT_HOW;
    const low = Number(match[1]);
    const high = match[2] === undefined ? low : Number(match[2]);
    if (!(100 <= low && low <= high && high <= 599)) return `${raw.trim()} is not a status range from 100 to 599.`;
  }
  return null;
}

/** Reads the health check form, with the backend's own wording for what it would refuse. */
export function parseHealth(draft: HealthDraft): { values: HealthValues; errors: HealthErrors } {
  const errors: HealthErrors = {};
  const path = draft.path.trim();
  const expect = draft.expect.trim();
  const timeout = draft.timeout.trim();

  if (path !== "") {
    const problem = pathProblem(path);
    if (problem !== null) errors.path = problem;
  }
  if (expect !== "") {
    const problem = expectProblem(expect);
    if (problem !== null) errors.expect = problem;
  }
  let seconds: number | null = null;
  if (timeout !== "") {
    seconds = /^\d+$/.test(timeout) ? Number.parseInt(timeout, 10) : Number.NaN;
    if (!(seconds >= HEALTH_TIMEOUT_MIN && seconds <= HEALTH_TIMEOUT_MAX)) {
      errors.timeout = `Give it from ${String(HEALTH_TIMEOUT_MIN)} to ${String(HEALTH_TIMEOUT_MAX)} seconds, or leave it empty for ${String(HEALTH_DEFAULTS.timeout)}.`;
    }
  }
  return {
    values: { path: path === "" ? null : path, expect: expect === "" ? null : expect, timeout: timeout === "" ? null : seconds },
    errors,
  };
}

/**
 * The field a backend refusal is about. The store's validator answers `400` with one sentence
 * and no `fields` map, so the field is read off the sentence it wrote; a `422` from the
 * request's own validation names it. Null when neither says.
 */
export function healthFieldOf(detail: string): keyof HealthDraft | null {
  if (/timeout/i.test(detail)) return "timeout";
  if (/status/i.test(detail)) return "expect";
  if (/\bpath\b|contains a space/i.test(detail)) return "path";
  return null;
}

function unset(value: unknown): boolean {
  return value === null || value === undefined;
}

/** What the gate asks now, each part marked when it is the default. */
export function effectiveHealth(app: Pick<App, "health_path" | "health_expect" | "health_timeout">): {
  path: string;
  expect: string;
  timeout: number;
  defaults: Record<keyof HealthDraft, boolean>;
} {
  return {
    path: app.health_path ?? HEALTH_DEFAULTS.path,
    expect: app.health_expect ?? HEALTH_DEFAULTS.expect,
    timeout: app.health_timeout ?? HEALTH_DEFAULTS.timeout,
    defaults: { path: unset(app.health_path), expect: unset(app.health_expect), timeout: unset(app.health_timeout) },
  };
}

/** Reads the retention field: a whole number of releases from 1 to 50. */
export function parseRetention(text: string): { keep: number | null; error: string | null } {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return { keep: null, error: `Enter a whole number of releases, from ${String(RETENTION_MIN)} to ${String(RETENTION_MAX)}.` };
  const keep = Number.parseInt(trimmed, 10);
  if (keep < RETENTION_MIN || keep > RETENTION_MAX) {
    return { keep: null, error: `Keep from ${String(RETENTION_MIN)} to ${String(RETENTION_MAX)} releases.` };
  }
  return { keep, error: null };
}

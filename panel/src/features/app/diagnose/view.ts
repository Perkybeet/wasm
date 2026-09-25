/**
 * How a diagnosis is drawn: the words and tones for its verdict and for each check, from the
 * vocabulary of `wasm.managers.diagnose` (verdicts `healthy`, `degraded`, `down`; checks `ok`,
 * `warn`, `fail`, `skip`). A word the console does not know is still shown, verbatim, in the
 * neutral tone: the backend's word beats a guess.
 */

import type { Diagnosis } from "../../../api/queries/apps";

export type Tone = "ok" | "warn" | "fail" | "idle";

export type Check = Diagnosis["checks"][number];

export interface StatusWord {
  tone: Tone;
  /** The word on screen, next to the shape: never the colour alone. */
  word: string;
}

function capitalise(text: string): string {
  const clean = text.trim().replace(/_/g, " ");
  return clean.charAt(0).toUpperCase() + clean.slice(1);
}

const VERDICTS: Readonly<Record<string, StatusWord>> = {
  healthy: { tone: "ok", word: "Healthy" },
  degraded: { tone: "warn", word: "Degraded" },
  down: { tone: "fail", word: "Down" },
};

/** The verdict as the console draws it. */
export function verdictView(verdict: string): StatusWord {
  return VERDICTS[verdict.trim().toLowerCase()] ?? { tone: "idle", word: capitalise(verdict) || "Unknown" };
}

const CHECK_STATUSES: Readonly<Record<string, StatusWord>> = {
  ok: { tone: "ok", word: "Passed" },
  warn: { tone: "warn", word: "Warning" },
  fail: { tone: "fail", word: "Failed" },
  skip: { tone: "idle", word: "Skipped" },
};

/** One check's status as the console draws it. */
export function checkStatus(status: string): StatusWord {
  return CHECK_STATUSES[status.trim().toLowerCase()] ?? { tone: "idle", word: capitalise(status) || "Unknown" };
}

/** What each probe looks at, in the interface's words. */
const CHECK_LABELS: Readonly<Record<string, string>> = {
  unit: "Service unit",
  port: "Listening port",
  http_direct: "HTTP, direct",
  http_nginx: "HTTP, through nginx",
  journal: "Journal",
  nginx_log: "Web server error log",
  certificate: "Certificate",
  last_deployment: "Last deploy",
  oom: "Out-of-memory kills",
  disk: "Disk space",
};

export function checkLabel(name: string): string {
  return CHECK_LABELS[name] ?? capitalise(name);
}

/** The sentence under the verdict when the checks name no single cause. */
export function causeFallback(verdict: string): string {
  switch (verdict.trim().toLowerCase()) {
    case "healthy":
      return "Everything WASM can check about this app answers as it should.";
    case "down":
      return "The app is down, and the checks do not point to one cause. Start with the failed ones below.";
    default:
      return "Some checks did not pass, and they do not point to one cause. Start with the failed ones below.";
  }
}

export interface Tally {
  ok: number;
  warn: number;
  fail: number;
  skip: number;
}

/** How many checks ended in each status; statuses the console does not know count as skipped. */
export function tally(checks: readonly Check[]): Tally {
  const counts: Tally = { ok: 0, warn: 0, fail: 0, skip: 0 };
  for (const check of checks) {
    const key = check.status.trim().toLowerCase();
    if (key === "ok" || key === "warn" || key === "fail") counts[key] += 1;
    else counts.skip += 1;
  }
  return counts;
}

/** A check worth reading first: it did not pass and has output to show. */
export function opensByDefault(check: Check): boolean {
  const status = check.status.trim().toLowerCase();
  return (status === "fail" || status === "warn") && check.evidence.trim() !== "";
}

/** What an operator says after a re-run, for the live region. */
export function verdictAnnouncement(domain: string, diagnosis: Diagnosis): string {
  const verdict = verdictView(diagnosis.verdict).word;
  const cause = diagnosis.probable_cause ?? causeFallback(diagnosis.verdict);
  return `${domain}: ${verdict}. ${cause}`;
}

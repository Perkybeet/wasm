/**
 * The five phases of a deploy (fetch, install, build, activate, health), read back from what
 * the deploy wrote down.
 *
 * Nothing in the API names the phase a deploy is in. What exists is text: the step headers the
 * deployer's logger prints into the captured build log (`[3/9] Building application...`), its
 * substeps (`→ Running: npm ci`, `→ Checking: http://127.0.0.1:3000/`), and the phases the
 * update job reports (`Pulling latest changes`, `Restarting`). Only those lines are read, never
 * the tools' own output, so a commit message that says "update dependencies" does not move the
 * timeline. A phase that never shows up in any of them is drawn as not recorded rather than
 * guessed.
 */

export type PhaseKey = "fetch" | "install" | "build" | "activate" | "health";

export interface PhaseSpec {
  key: PhaseKey;
  label: string;
  /** What is happening while it runs, for the announcement: "Building". */
  doing: string;
}

export const PHASES: readonly PhaseSpec[] = [
  { key: "fetch", label: "Fetch", doing: "Fetching the source" },
  { key: "install", label: "Install", doing: "Installing dependencies" },
  { key: "build", label: "Build", doing: "Building" },
  { key: "activate", label: "Activate", doing: "Activating" },
  { key: "health", label: "Health", doing: "Checking health" },
];

const INDEX: Readonly<Record<PhaseKey, number>> = { fetch: 0, install: 1, build: 2, activate: 3, health: 4 };

/**
 * Most specific first: a health probe line mentions nothing else, an activation line may name
 * a release, a build line may be an npm command.
 */
const RULES: readonly (readonly [PhaseKey, RegExp])[] = [
  ["health", /\bhealth\b|^checking: https?:\/\//i],
  ["activate", /\bactivat|\brestart|\bstarting application\b|\bswitch(?:ing|ed)? to release\b/i],
  ["build", /\bbuild(?:ing)?\b|\bcompil/i],
  ["install", /\binstall|\bdependenc|\b(?:npm|pnpm|yarn|bun) ci\b|\breused\b/i],
  ["fetch", /\bfetch|\bpull(?:ing)?\b|\bclon(?:e|ing)\b|\bhead is now at\b|^source:/i],
];

/** The phase a step names, or null for a step that belongs to none (permissions, the site). */
export function phaseOf(step: string): PhaseKey | null {
  const text = step.trim();
  for (const [key, pattern] of RULES) if (pattern.test(text)) return key;
  return null;
}

const HEADER = /^\[\d+\/\d+\]\s*(.*)$/;
const ARROW = /^==>\s*(.*)$/;
const SUBSTEP = /^→\s*(.*)$/;

/**
 * The step a line of a captured build log announces, without its decoration, or null when the
 * line is a tool's own output. Step headers carry an icon before the title, which is dropped.
 */
export function stepOf(line: string): string | null {
  const text = line.trim();
  const header = HEADER.exec(text);
  if (header) {
    // "[3/9] 🔨 Building application..." -> "Building application"
    return (header[1] ?? "").replace(/^[^\p{L}\p{N}]+/u, "").replace(/\.{3}$/, "");
  }
  const arrow = ARROW.exec(text) ?? SUBSTEP.exec(text);
  return arrow ? (arrow[1] ?? "") : null;
}

export interface PhaseEvent {
  phase: PhaseKey;
  /** When the line was written, when the log says. */
  at: Date | null;
}

/** How the deploy ended, in the timeline's terms. */
export type Outcome = "running" | "succeeded" | "failed";

export type PhaseState = "done" | "running" | "failed" | "pending" | "unrecorded";

export interface PhaseView extends PhaseSpec {
  state: PhaseState;
  startedAt: Date | null;
  /** How long it took, or has taken so far; null without times. */
  seconds: number | null;
}

/** The deployment status words as the timeline reads them. */
export function outcomeOf(status: string | null | undefined): Outcome {
  switch (status) {
    case "queued":
    case "running":
    case "pending":
      return "running";
    case "failed":
    case "cancelled":
      return "failed";
    default:
      return "succeeded";
  }
}

/**
 * Lays the five phases out from the steps a deploy recorded.
 *
 * A phase that appeared is done once a later one appeared; the furthest one reached is running,
 * failed or done by how the deploy ended. Phases are only ever reached forwards: a late mention
 * of an earlier phase (going back to the previous release after a failed health check) does not
 * move the deploy back. A phase before the furthest one that never appeared, or one after it in
 * a deploy that finished, is "unrecorded": it may have run without a line, or been skipped (a
 * static site installs nothing), and the log does not say which.
 */
export function timeline(
  events: readonly PhaseEvent[],
  outcome: Outcome,
  finishedAt: Date | null,
  now: Date,
): PhaseView[] {
  const first = new Map<PhaseKey, Date | null>();
  let reached = -1;
  for (const event of events) {
    if (!first.has(event.phase)) first.set(event.phase, event.at);
    reached = Math.max(reached, INDEX[event.phase]);
  }

  const starts = PHASES.map((spec) => first.get(spec.key) ?? null);
  return PHASES.map((spec, index) => {
    const seen = first.has(spec.key);
    let state: PhaseState;
    if (index < reached) state = seen ? "done" : "unrecorded";
    else if (index === reached) state = outcome === "running" ? "running" : outcome === "failed" ? "failed" : "done";
    else state = outcome === "running" ? "pending" : "unrecorded";

    const startedAt = starts[index] ?? null;
    let seconds: number | null = null;
    if (startedAt !== null && (state === "done" || state === "running" || state === "failed")) {
      const next = starts.slice(index + 1).find((start): start is Date => start !== null && start >= startedAt);
      const end = next ?? (state === "running" ? now : finishedAt);
      if (end) seconds = Math.max(0, (end.getTime() - startedAt.getTime()) / 1000);
    }
    return { ...spec, state, startedAt, seconds };
  });
}

/** The phase a deploy is in now, for the announcement; null before the first step. */
export function currentPhase(views: readonly PhaseView[]): PhaseView | null {
  return views.find((view) => view.state === "running" || view.state === "failed") ?? null;
}

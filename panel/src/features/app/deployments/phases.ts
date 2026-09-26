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
  /** When the line was written, on the logs' clock (see `logClock` in buildLog.ts). */
  at: Date | null;
}

/** How the deploy ended, in the timeline's terms. */
export type Outcome = "running" | "succeeded" | "failed";

export type PhaseState = "done" | "running" | "failed" | "pending" | "unrecorded" | "not_applicable";

export interface PhaseView extends PhaseSpec {
  state: PhaseState;
  /** When it started, as an instant; null without times, or when the logs' clock cannot be placed. */
  startedAt: Date | null;
  /** How long it took, or has taken so far; null without times. */
  seconds: number | null;
}

/**
 * What the timeline measures against, all read off the logs except `now`.
 *
 * The logs stamp the server's wall clock without a zone, while `started_at` and the browser's
 * clock are instants. Mixing the two is how a ten-second deploy once read "Activate 1h 0m" in
 * a browser an hour away from the server. So a phase is measured only by differences between
 * log times, and the end of the last one is the last line written, never `finished_at`. Only a
 * phase still running needs the present, and that is the one place the offset is applied.
 */
export interface PhaseClock {
  /** The last line either log wrote, on the logs' clock: where a finished deploy's last phase ends. */
  lastAt: Date | null;
  /** The present, as an instant. */
  now: Date;
  /** Milliseconds the logs' clock is ahead of UTC (`logClockOffset`); null when unknown. */
  offset: number | null;
}

/** Every zone in use is a whole number of quarter hours from UTC. */
const QUARTER_HOUR_MS = 15 * 60_000;

/**
 * How far the logs' clock is ahead of UTC, in milliseconds: the deploy's first line against its
 * `started_at`, which the store keeps as an instant. The first line is written within moments
 * of the start, so the difference, rounded to the quarter hour, is the server's zone offset
 * and nothing else. Null when either is missing.
 */
export function logClockOffset(firstAt: Date | null, startedAt: Date | null): number | null {
  if (firstAt === null || startedAt === null) return null;
  return Math.round((firstAt.getTime() - startedAt.getTime()) / QUARTER_HOUR_MS) * QUARTER_HOUR_MS;
}

export interface TimelineOptions {
  /**
   * Whether the deploy has a health phase at all. A static site is served as files by the web
   * server: there is no process to probe, so its phase is not applicable rather than missing.
   */
  checksHealth?: boolean;
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
 *
 * Durations come from the logs' own clock only (see PhaseClock).
 */
export function timeline(
  events: readonly PhaseEvent[],
  outcome: Outcome,
  clock: PhaseClock,
  { checksHealth = true }: TimelineOptions = {},
): PhaseView[] {
  const first = new Map<PhaseKey, Date | null>();
  let reached = -1;
  for (const event of events) {
    if (!checksHealth && event.phase === "health") continue;
    if (!first.has(event.phase)) first.set(event.phase, event.at);
    reached = Math.max(reached, INDEX[event.phase]);
  }

  const nowOnLogClock = clock.offset === null ? null : new Date(clock.now.getTime() + clock.offset);
  const starts = PHASES.map((spec) => first.get(spec.key) ?? null);
  return PHASES.map((spec, index) => {
    if (!checksHealth && spec.key === "health") return { ...spec, state: "not_applicable", startedAt: null, seconds: null };
    const seen = first.has(spec.key);
    let state: PhaseState;
    if (index < reached) state = seen ? "done" : "unrecorded";
    else if (index === reached) state = outcome === "running" ? "running" : outcome === "failed" ? "failed" : "done";
    else state = outcome === "running" ? "pending" : "unrecorded";

    const started = starts[index] ?? null;
    let seconds: number | null = null;
    if (started !== null && (state === "done" || state === "running" || state === "failed")) {
      const next = starts.slice(index + 1).find((start): start is Date => start !== null && start >= started);
      const end = next ?? (state === "running" ? nowOnLogClock : clock.lastAt);
      if (end) seconds = Math.max(0, (end.getTime() - started.getTime()) / 1000);
    }
    const startedAt = started === null || clock.offset === null ? null : new Date(started.getTime() - clock.offset);
    return { ...spec, state, startedAt, seconds };
  });
}

/** The phase a deploy is in now, for the announcement; null before the first step. */
export function currentPhase(views: readonly PhaseView[]): PhaseView | null {
  return views.find((view) => view.state === "running" || view.state === "failed") ?? null;
}

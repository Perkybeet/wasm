/**
 * Reading the two logs a deploy leaves: the captured build log (`[2026-09-25 21:53:39] line`,
 * written by the deployment recorder) and its job's log (`[2026-09-25 21:53:39] [INFO] step`,
 * written by the job manager, or the same entries over the job's WebSocket). Both become log
 * viewer lines with the time in its own column and the text verbatim.
 */

import { useState } from "react";

import type { LogLine } from "../../../components/ui/LogViewer";
import type { PhaseEvent } from "./phases";
import { phaseOf, stepOf } from "./phases";

export interface BuildLogLine extends LogLine {
  /** When it was written, on the logs' clock (see logClock), when the log says. */
  at: Date | null;
}

const WALL = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?/;

/**
 * A time a log wrote, on the logs' clock: the server's wall clock, read as if it were UTC.
 *
 * Both logs stamp the server's local time with no zone (`datetime.now()`), and the browser
 * does not know the server's zone, so reading them as the browser's local time placed them
 * wherever the operator happened to be. Read as UTC they keep exactly the differences between
 * them, which is all a phase's duration needs; `logClockOffset` (phases.ts) places them in
 * time when the present is needed. Any zone suffix is ignored: the digits are what the server's
 * clock said, like every other line of the same log.
 */
export function logClock(text: string): Date | null {
  const match = WALL.exec(text.trim());
  if (!match) return null;
  const [, date, time, fraction] = match;
  const millis = fraction === undefined ? "" : `.${fraction.slice(0, 3).padEnd(3, "0")}`;
  const parsed = new Date(`${date ?? ""}T${time ?? ""}${millis}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** "21:53:39": a time on the logs' clock as the server wrote it. */
export function logClockText(at: Date): string {
  return at.toISOString().slice(11, 19);
}

/** The earliest and the latest of some log times, ignoring the lines that had none. */
export function logSpan(times: Iterable<Date | null>): { first: Date | null; last: Date | null } {
  let first: Date | null = null;
  let last: Date | null = null;
  for (const at of times) {
    if (at === null) continue;
    if (first === null || at < first) first = at;
    if (last === null || at > last) last = at;
  }
  return { first, last };
}

const STAMP = /^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] ?(.*)$/;
const JOB_LEVEL = /^\[(DEBUG|INFO|SUCCESS|WARNING|WARN|ERROR)\] ?(.*)$/;

// systemd's own failure words are included: "Failed with result 'exit-code'", "status=1/FAILURE".
const ERROR =
  /npm err!|\berr!|\berror\b|✗|failed to compile|\btype error\b|\btraceback\b|\bexception\b|\bfatal\b|\bfailed with result\b|\/failure\b/i;
const WARNING = /\bwarn(?:ing)?\b|⚠/i;

/**
 * The level a line is drawn at. The tools do not say; the words do. Deliberately narrow: a
 * line is only coloured when it reads as a failure or a warning to a person too.
 */
export function levelOf(text: string): LogLine["level"] {
  if (ERROR.test(text)) return "error";
  if (WARNING.test(text)) return "warn";
  return undefined;
}

function jobLevel(word: string): LogLine["level"] {
  switch (word) {
    case "ERROR":
      return "error";
    case "WARNING":
    case "WARN":
      return "warn";
    case "DEBUG":
      return "debug";
    default:
      return undefined;
  }
}

/** One line of either log, split into its time and its text. */
export function parseLine(raw: string, id: number): BuildLogLine {
  const stamped = STAMP.exec(raw);
  if (!stamped) return { id, text: raw, at: null, ...optionalLevel(levelOf(raw)) };
  const [, date, time, rest = ""] = stamped;
  const at = logClock(`${date ?? ""}T${time ?? ""}`);
  const job = JOB_LEVEL.exec(rest);
  if (job) {
    const text = job[2] ?? "";
    return { id, text, ts: time ?? "", at, ...optionalLevel(jobLevel(job[1] ?? "") ?? levelOf(text)) };
  }
  return { id, text: rest, ts: time ?? "", at, ...optionalLevel(levelOf(rest)) };
}

function optionalLevel(level: LogLine["level"]): { level?: NonNullable<LogLine["level"]> } {
  return level === undefined ? {} : { level };
}

/** Splits a log file into lines; a trailing newline does not make an empty last line. */
export function parseLog(content: string, firstId = 0): BuildLogLine[] {
  if (content === "") return [];
  const raw = content.endsWith("\n") ? content.slice(0, -1) : content;
  return raw.split("\n").map((line, index) => parseLine(line, firstId + index));
}

/**
 * The lines of a log that is being re-read as it grows. When the new text extends the old,
 * only the new lines are parsed and the old line objects are kept, so the viewer, which caches
 * its work per line object, does not start over every second.
 */
export function useLogLines(content: string | undefined): BuildLogLine[] {
  const [memo, setMemo] = useState<{ content: string; lines: BuildLogLine[] }>({ content: "", lines: [] });
  if (content === undefined || content === memo.content) return memo.lines;
  // Adjusted while rendering, not after, so the viewer never draws the stale lines.
  const grew = memo.content !== "" && memo.content.endsWith("\n") && content.startsWith(memo.content);
  const lines = grew
    ? memo.lines.concat(parseLog(content.slice(memo.content.length), memo.lines.length))
    : parseLog(content);
  setMemo({ content, lines });
  return lines;
}

/** The phase steps a captured build log recorded, in order. */
export function buildLogEvents(lines: readonly BuildLogLine[]): PhaseEvent[] {
  const events: PhaseEvent[] = [];
  for (const line of lines) {
    const step = stepOf(line.text);
    const phase = step === null ? null : phaseOf(step);
    if (phase !== null) events.push({ phase, at: line.at });
  }
  return events;
}

/** A job reports nothing but steps, so every one of its lines is read. */
export function jobEvents(entries: readonly { text: string; at: Date | null }[]): PhaseEvent[] {
  const events: PhaseEvent[] = [];
  for (const entry of entries) {
    const phase = phaseOf(entry.text);
    if (phase !== null) events.push({ phase, at: entry.at });
  }
  return events;
}

/** Both logs' steps as one sequence, in the order they happened. */
export function mergeEvents(...sources: readonly PhaseEvent[][]): PhaseEvent[] {
  const all = sources.flat();
  // Stable: events without a time keep their place relative to their own source.
  return all
    .map((event, order) => ({ event, order }))
    .sort((a, b) => {
      const at = a.event.at?.getTime();
      const bt = b.event.at?.getTime();
      if (at === undefined || bt === undefined) return a.order - b.order;
      return at - bt || a.order - b.order;
    })
    .map(({ event }) => event);
}

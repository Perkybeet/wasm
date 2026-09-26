// The browser an hour east of UTC, as the owner's is in winter (Etc/GMT-1 is UTC+1: POSIX
// inverts the sign). Set before any Date is made; Node reads TZ again when it changes.
const { process } = globalThis as unknown as { process: { env: Record<string, string | undefined> } };
process.env["TZ"] = "Etc/GMT-1";

import { describe, expect, it } from "vitest";

import { buildLogEvents, logSpan, parseLog } from "./buildLog";
import { logClockOffset, timeline } from "./phases";

/** A static deploy of ten seconds, logged by a server on UTC, as the recorder writes it. */
const STATIC_LOG = [
  "[2026-09-26 10:00:00] [1/6] Fetching source code...",
  "[2026-09-26 10:00:02]       → HEAD is now at 9f2c41a",
  "[2026-09-26 10:00:03] [2/6] Building application...",
  "[2026-09-26 10:00:06] [5/6] Activating release...",
  "[2026-09-26 10:00:10] ✓ Deployed landing.example.com",
].join("\n");

const STARTED_AT = "2026-09-26T10:00:00+00:00";

describe("phase timings in a browser at UTC+1", () => {
  it("runs in the zone it claims to", () => {
    expect(new Date("2026-09-26T10:00:00Z").getTimezoneOffset()).toBe(-60);
  });

  it("never lets the zone into a finished deploy's durations", () => {
    const lines = parseLog(STATIC_LOG);
    const span = logSpan(lines.map((line) => line.at));
    const views = timeline(
      buildLogEvents(lines),
      "succeeded",
      { lastAt: span.last, now: new Date("2026-09-26T12:00:00Z"), offset: logClockOffset(span.first, new Date(STARTED_AT)) },
      { checksHealth: false },
    );
    const seconds = Object.fromEntries(views.map((view) => [view.key, view.seconds]));
    expect(seconds).toEqual({ fetch: 3, install: null, build: 3, activate: 4, health: null });
    expect(views.find((view) => view.key === "activate")?.startedAt?.toISOString()).toBe("2026-09-26T10:00:06.000Z");
  });

  it("measures a running phase from the log against the present, whatever the server's zone", () => {
    // The server is on UTC+2 this time: its 12:00:03 is 10:00:03Z.
    const lines = parseLog(["[2026-09-26 12:00:00] [1/6] Fetching source code...", "[2026-09-26 12:00:03] [2/6] Building application..."].join("\n"));
    const span = logSpan(lines.map((line) => line.at));
    const views = timeline(buildLogEvents(lines), "running", {
      lastAt: span.last,
      now: new Date("2026-09-26T10:00:33Z"),
      offset: logClockOffset(span.first, new Date(STARTED_AT)),
    });
    expect(views.find((view) => view.key === "build")).toMatchObject({ state: "running", seconds: 30 });
  });
});

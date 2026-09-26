import { describe, expect, it } from "vitest";

import { buildLogEvents, jobEvents, levelOf, logClock, mergeEvents, parseLine, parseLog } from "./buildLog";
import { currentPhase, logClockOffset, outcomeOf, phaseOf, stepOf, timeline } from "./phases";
import type { PhaseClock } from "./phases";

/** A time on the logs' clock, as parseLine reads one. */
const at = (time: string) => new Date(`2026-09-25T${time}Z`);

/** A finished deploy's clock: its last line, and a server on UTC. */
const ended = (last: Date | null): PhaseClock => ({ lastAt: last, now: at("13:00:00"), offset: 0 });

describe("stepOf", () => {
  it("reads the deployer's step headers without the counter, the icon or the ellipsis", () => {
    expect(stepOf("[3/9] 🔨 Building application...")).toBe("Building application");
    expect(stepOf("[7/9] ⚙️ Creating systemd service...")).toBe("Creating systemd service");
  });

  it("reads substeps and the ==> convention", () => {
    expect(stepOf("      → Running: npm ci")).toBe("Running: npm ci");
    expect(stepOf("==> Health check passed: GET / answered 200")).toBe("Health check passed: GET / answered 200");
  });

  it("ignores the tools' own output", () => {
    expect(stepOf("added 812 packages, and audited 813 packages in 13s")).toBeNull();
    expect(stepOf("HEAD is now at 9f2c41a Update dependencies")).toBeNull();
    expect(stepOf("      [DEBUG] npm warn deprecated inflight@1.0.6")).toBeNull();
  });
});

describe("phaseOf", () => {
  it.each([
    ["Fetching source code", "fetch"],
    ["Fetching source into a new release", "fetch"],
    ["Pulling latest changes", "fetch"],
    ["Fetching the recorded source again", "fetch"],
    ["HEAD is now at c07d5e3", "fetch"],
    ["Installing dependencies", "install"],
    ["Running: npm ci", "install"],
    ["Running: pip install -r requirements.txt", "install"],
    ["Dependencies reused from 20260924-101500-9f8e7d6: the lockfile did not change", "install"],
    ["Building application", "build"],
    ["Building", "build"],
    ["Running: npm run build", "build"],
    ["Activating release", "activate"],
    ["Activated release 20260925-143012-a1b2c3d (was 20260924-101500-9f8e7d6)", "activate"],
    ["Restarting", "activate"],
    ["Starting application", "activate"],
    ["Checking: http://127.0.0.1:3000/", "health"],
    ["Health check passed", "health"],
  ] as const)("%s is %s", (step, phase) => {
    expect(phaseOf(step)).toBe(phase);
  });

  it.each(["Rebuilding", "Setting permissions", "Creating site configuration", "Detecting application type", "Creating pre-update backup", "Deployment complete"])(
    "%s belongs to no phase",
    (step) => {
      expect(phaseOf(step)).toBeNull();
    },
  );
});

describe("timeline", () => {
  const now: PhaseClock = { lastAt: null, now: at("12:10:00"), offset: 0 };

  it("marks what came before the furthest phase done, with durations from the times", () => {
    const views = timeline(
      [
        { phase: "fetch", at: at("12:00:00") },
        { phase: "install", at: at("12:00:04") },
        { phase: "build", at: at("12:00:20") },
      ],
      "running",
      now,
    );
    expect(views.map((view) => view.state)).toEqual(["done", "done", "running", "pending", "pending"]);
    expect(views[0]?.seconds).toBe(4);
    expect(views[1]?.seconds).toBe(16);
    // Still running: measured up to now.
    expect(views[2]?.seconds).toBe(580);
    expect(currentPhase(views)?.key).toBe("build");
  });

  it("puts the failure on the furthest phase reached and leaves the rest not reached", () => {
    const views = timeline(
      [
        { phase: "fetch", at: at("12:00:00") },
        { phase: "build", at: at("12:00:20") },
      ],
      "failed",
      ended(at("12:00:45")),
    );
    expect(views.map((view) => view.state)).toEqual(["done", "unrecorded", "failed", "unrecorded", "unrecorded"]);
    expect(views[2]?.seconds).toBe(25);
  });

  it("never walks back: going back to the previous release after a failed health check stays in health", () => {
    const views = timeline(
      [
        { phase: "activate", at: at("12:00:30") },
        { phase: "health", at: at("12:00:31") },
        { phase: "activate", at: at("12:01:01") },
      ],
      "failed",
      ended(at("12:01:04")),
    );
    expect(views[4]?.state).toBe("failed");
    expect(views[3]?.state).toBe("done");
  });

  it("says nothing it does not know when a finished deploy recorded no steps", () => {
    expect(timeline([], "succeeded", ended(null)).map((view) => view.state)).toEqual(Array(5).fill("unrecorded"));
    expect(timeline([], "running", now).map((view) => view.state)).toEqual(Array(5).fill("pending"));
  });

  it("ends a finished deploy's last phase at its last line, not at finished_at", () => {
    const views = timeline(
      [
        { phase: "fetch", at: at("12:00:00") },
        { phase: "activate", at: at("12:00:08") },
      ],
      "succeeded",
      ended(at("12:00:10")),
      { checksHealth: false },
    );
    expect(views[3]?.seconds).toBe(2);
  });

  it("gives a static site's health phase as not applicable, never as missing from the log", () => {
    const events = [
      { phase: "fetch", at: at("12:00:00") },
      { phase: "activate", at: at("12:00:08") },
    ] as const;
    expect(timeline(events, "succeeded", ended(at("12:00:10")), { checksHealth: false })[4]?.state).toBe("not_applicable");
    expect(timeline(events, "running", now, { checksHealth: false })[4]?.state).toBe("not_applicable");
    // With a health check, the same log leaves it unrecorded.
    expect(timeline(events, "succeeded", ended(at("12:00:10")))[4]?.state).toBe("unrecorded");
  });

  it("places phases in time through the offset, and measures a running one against the present", () => {
    // The server's clock is an hour ahead of UTC: its 13:00:00 is 12:00:00Z.
    const views = timeline([{ phase: "build", at: at("13:00:00") }], "running", {
      lastAt: null,
      now: new Date("2026-09-25T12:00:30Z"),
      offset: 3_600_000,
    });
    expect(views[2]?.seconds).toBe(30);
    expect(views[2]?.startedAt?.toISOString()).toBe("2026-09-25T12:00:00.000Z");
  });
});

describe("logClockOffset", () => {
  it("is the server's zone offset, rounded to the quarter hour", () => {
    const started = new Date("2026-09-25T12:00:00Z");
    expect(logClockOffset(at("12:00:01"), started)).toBe(0);
    expect(logClockOffset(at("14:00:02"), started)).toBe(2 * 3_600_000);
    expect(logClockOffset(at("06:30:00"), started)).toBe(-5.5 * 3_600_000);
    expect(logClockOffset(null, started)).toBeNull();
  });
});

describe("outcomeOf", () => {
  it("reads deployment and job words", () => {
    expect(outcomeOf("queued")).toBe("running");
    expect(outcomeOf("running")).toBe("running");
    expect(outcomeOf("failed")).toBe("failed");
    expect(outcomeOf("success")).toBe("succeeded");
    expect(outcomeOf("rolled_back")).toBe("succeeded");
  });
});

describe("the logs", () => {
  it("splits a recorder line into its time and its verbatim text", () => {
    const line = parseLine("[2026-09-25 21:53:39]       → Running: npm ci", 7);
    expect(line).toMatchObject({ id: 7, ts: "21:53:39", text: "      → Running: npm ci" });
    // On the logs' clock: the digits the server wrote, whatever zone the browser is in.
    expect(line.at?.getUTCHours()).toBe(21);
  });

  it("reads a job entry's ISO time on the same clock, ignoring any zone suffix", () => {
    expect(logClock("2026-09-25T21:53:39.123456")?.toISOString()).toBe("2026-09-25T21:53:39.123Z");
    expect(logClock("2026-09-25T21:53:39+02:00")?.toISOString()).toBe("2026-09-25T21:53:39.000Z");
    expect(logClock("yesterday")).toBeNull();
  });

  it("reads a job log line's level and message", () => {
    expect(parseLine("[2026-09-25 21:53:45] [INFO] Restarting", 0)).toMatchObject({ text: "Restarting", ts: "21:53:45" });
    expect(parseLine("[2026-09-25 21:53:45] [ERROR] Update failed", 0).level).toBe("error");
  });

  it("colours only what reads as a failure or a warning", () => {
    expect(levelOf("npm ERR! code ELIFECYCLE")).toBe("error");
    expect(levelOf("Type error: Property 'total' does not exist on type 'Order'.")).toBe("error");
    expect(levelOf("npm warn deprecated glob@7.2.3")).toBe("warn");
    expect(levelOf("found 0 vulnerabilities")).toBeUndefined();
    expect(levelOf("wasm-shop.service: Failed with result 'exit-code'.")).toBe("error");
    expect(levelOf("wasm-shop.service: Main process exited, code=exited, status=1/FAILURE")).toBe("error");
    expect(levelOf("Checked 3 files, 0 errors")).toBeUndefined();
  });

  it("does not make an empty line of the final newline, and keeps unstamped lines verbatim", () => {
    expect(parseLog("a\nb\n").map((line) => line.text)).toEqual(["a", "b"]);
    expect(parseLog("==> Building\n")[0]).toMatchObject({ text: "==> Building", at: null });
  });

  it("merges the build log's steps and the job's by time", () => {
    const lines = parseLog(
      [
        "[2026-09-25 12:00:04]       → Running: npm ci",
        "[2026-09-25 12:00:04] added 214 packages",
        "[2026-09-25 12:00:20]       → Running: npm run build",
      ].join("\n"),
    );
    const job = jobEvents([
      { text: "Pulling latest changes", at: at("12:00:01") },
      { text: "Rebuilding", at: at("12:00:03") },
      { text: "Restarting", at: at("12:00:40") },
    ]);
    const events = mergeEvents(buildLogEvents(lines), job);
    expect(events.map((event) => event.phase)).toEqual(["fetch", "install", "build", "activate"]);
  });
});

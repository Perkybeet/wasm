import { describe, expect, it } from "vitest";

import { STATE_RANK, appStatus, deployStatus } from "./status";

describe("appStatus", () => {
  it.each([
    // GET /api/apps and the `app` event.
    ["running", "running", "Running", false],
    ["stopped", "stopped", "Stopped", false],
    ["static", "static", "Static", false],
    ["deploying", "deploying", "Deploying", false],
    // The store's AppStatus.
    ["failed", "failed", "Failed", true],
    ["unknown", "unknown", "Unknown", true],
    // wasm.core.app_state, as `wasm list` prints it.
    ["Running", "running", "Running", false],
    ["Restarting", "deploying", "Restarting", true],
    ["No answer", "failed", "No answer", true],
    ["Stopped", "stopped", "Stopped", false],
    ["Failed", "failed", "Failed", true],
    ["Static", "static", "Static", false],
  ])("%s is drawn %s, labelled %s, attention %s", (status, state, label, attention) => {
    expect(appStatus(status)).toEqual({ state, label, attention });
  });

  it("shows a word it does not know verbatim, with the unknown shape", () => {
    expect(appStatus("degraded")).toEqual({ state: "unknown", label: "Degraded", attention: false });
  });

  it("says Unknown when there is no word at all", () => {
    expect(appStatus(null).label).toBe("Unknown");
    expect(appStatus(undefined).label).toBe("Unknown");
    expect(appStatus("  ").label).toBe("Unknown");
  });
});

describe("deployStatus", () => {
  it.each([
    ["queued", "deploying", "Queued"],
    ["running", "deploying", "In progress"],
    ["success", "running", "Succeeded"],
    ["failed", "failed", "Failed"],
    ["rolled_back", "stopped", "Rolled back"],
    ["pending", "deploying", "Queued"],
    ["completed", "running", "Succeeded"],
    ["cancelled", "stopped", "Cancelled"],
  ])("%s is drawn %s and labelled %s", (status, state, label) => {
    expect(deployStatus(status)).toMatchObject({ state, label });
  });

  it("flags failures and rollbacks for attention", () => {
    expect(deployStatus("failed").attention).toBe(true);
    expect(deployStatus("rolled_back").attention).toBe(true);
    expect(deployStatus("success").attention).toBe(false);
  });

  it("keeps an unknown word readable", () => {
    expect(deployStatus("half_done").label).toBe("Half done");
  });
});

describe("STATE_RANK", () => {
  it("puts problems first and quiet states last", () => {
    expect(STATE_RANK.failed).toBeLessThan(STATE_RANK.deploying);
    expect(STATE_RANK.deploying).toBeLessThan(STATE_RANK.running);
    expect(STATE_RANK.running).toBeLessThan(STATE_RANK.stopped);
  });
});

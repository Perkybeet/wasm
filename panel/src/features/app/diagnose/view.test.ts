import { describe, expect, it } from "vitest";

import { causeFallback, checkLabel, checkStatus, opensByDefault, tally, verdictAnnouncement, verdictView } from "./view";

const CHECK = { name: "port", status: "fail", summary: "Nothing is listening on port 3000", evidence: "" };

describe("verdictView", () => {
  it("draws the three verdicts in the state language", () => {
    expect(verdictView("healthy")).toEqual({ tone: "ok", word: "Healthy" });
    expect(verdictView("degraded")).toEqual({ tone: "warn", word: "Degraded" });
    expect(verdictView("down")).toEqual({ tone: "fail", word: "Down" });
  });

  it("shows an unknown verdict verbatim, in the neutral tone", () => {
    expect(verdictView("half_up")).toEqual({ tone: "idle", word: "Half up" });
  });
});

describe("checkStatus", () => {
  it("names every status with a word, not only a colour", () => {
    expect(checkStatus("ok").word).toBe("Passed");
    expect(checkStatus("warn").word).toBe("Warning");
    expect(checkStatus("fail").word).toBe("Failed");
    expect(checkStatus("skip").word).toBe("Skipped");
    expect(checkStatus("flaky")).toEqual({ tone: "idle", word: "Flaky" });
  });
});

describe("checkLabel", () => {
  it("says what each probe looks at, and falls back to the probe's own name", () => {
    expect(checkLabel("http_nginx")).toBe("HTTP, through nginx");
    expect(checkLabel("last_deployment")).toBe("Last deploy");
    expect(checkLabel("swap_usage")).toBe("Swap usage");
  });
});

describe("tally", () => {
  it("counts each status, unknown ones as skipped", () => {
    const checks = [CHECK, { ...CHECK, status: "ok" }, { ...CHECK, status: "ok" }, { ...CHECK, status: "warn" }, { ...CHECK, status: "?" }];
    expect(tally(checks)).toEqual({ ok: 2, warn: 1, fail: 1, skip: 1 });
  });
});

describe("opensByDefault", () => {
  it("opens the output of what did not pass, when there is output", () => {
    expect(opensByDefault({ ...CHECK, evidence: "LISTEN 0 511 127.0.0.1:3001" })).toBe(true);
    expect(opensByDefault({ ...CHECK, status: "warn", evidence: "x" })).toBe(true);
    expect(opensByDefault({ ...CHECK, evidence: "  " })).toBe(false);
    expect(opensByDefault({ ...CHECK, status: "ok", evidence: "x" })).toBe(false);
  });
});

describe("what is said", () => {
  it("has a sentence when no single cause is named", () => {
    expect(causeFallback("healthy")).toMatch(/answers as it should/);
    expect(causeFallback("down")).toMatch(/^The app is down/);
  });

  it("announces the verdict and the cause after a re-run", () => {
    expect(verdictAnnouncement("shop.example.com", { domain: "shop.example.com", verdict: "down", probable_cause: "Port mismatch.", checks: [] })).toBe(
      "shop.example.com: Down. Port mismatch.",
    );
  });
});

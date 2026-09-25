import { describe, expect, it } from "vitest";

import { checkView, verdictView } from "./data";

describe("verdictView", () => {
  it("draws the three verdicts collect_health_report can answer", () => {
    expect(verdictView("healthy")).toEqual({ state: "running", label: "Healthy" });
    expect(verdictView("warning")).toEqual({ state: "deploying", label: "Needs attention" });
    expect(verdictView("error")).toEqual({ state: "failed", label: "Critical" });
  });

  it("shows an unrecognised verdict verbatim rather than guessing", () => {
    expect(verdictView("mystery")).toEqual({ state: "unknown", label: "mystery" });
  });
});

describe("checkView", () => {
  it("draws each HealthCheck status", () => {
    expect(checkView("ok")).toEqual({ state: "running", label: "OK" });
    expect(checkView("warning")).toEqual({ state: "deploying", label: "Warning" });
    expect(checkView("error")).toEqual({ state: "failed", label: "Error" });
    expect(checkView("info")).toEqual({ state: "unknown", label: "Info" });
  });
});

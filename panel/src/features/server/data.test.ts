import { describe, expect, it } from "vitest";

import { checkName, checkView, verdictView } from "./data";

describe("verdictView", () => {
  it("draws the three verdicts collect_health_report can answer", () => {
    expect(verdictView("healthy")).toEqual({ state: "running", label: "Healthy" });
    expect(verdictView("warning")).toEqual({ state: "warning", label: "Needs attention" });
    expect(verdictView("error")).toEqual({ state: "failed", label: "Critical" });
  });

  it("shows an unrecognised verdict verbatim rather than guessing", () => {
    expect(verdictView("mystery")).toEqual({ state: "unknown", label: "mystery" });
  });
});

describe("checkView", () => {
  it("draws each HealthCheck status", () => {
    expect(checkView("ok")).toEqual({ state: "running", label: "OK" });
    expect(checkView("warning")).toEqual({ state: "warning", label: "Warning" });
    expect(checkView("error")).toEqual({ state: "failed", label: "Error" });
    expect(checkView("info")).toEqual({ state: "unknown", label: "Info" });
  });
});

describe("checkName", () => {
  it("writes the CLI's check names in sentence case, and leaves others as sent", () => {
    expect(checkName("Disk Space")).toBe("Disk space");
    expect(checkName("SSL Certificates")).toBe("SSL certificates");
    expect(checkName("Nginx")).toBe("Nginx");
  });
});

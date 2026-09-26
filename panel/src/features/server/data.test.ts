import { describe, expect, it } from "vitest";

import { certificateMention, checkName, checkView, healthReasons, verdictView } from "./data";

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

describe("healthReasons", () => {
  it("lists the issues, then the warnings, in the report's own words", () => {
    const reasons = healthReasons({
      issues: ["Nginx is installed but not running"],
      warnings: ["App 'shop.example.com' - the unit is not running"],
    });
    expect(reasons).toEqual([
      { level: "issue", message: "Nginx is installed but not running", certificate: null },
      { level: "warning", message: "App 'shop.example.com' - the unit is not running", certificate: null },
    ]);
    expect(healthReasons({ issues: [], warnings: [] })).toEqual([]);
  });

  it("finds the certificate a message names, keeping the sentence around it intact", () => {
    expect(certificateMention("Certificate for shop.example.com expired 3 days ago")).toEqual({
      before: "Certificate for ",
      name: "shop.example.com",
      after: " expired 3 days ago",
    });
    expect(certificateMention("Certificate for shop.example.com-0001 expires in 5 days")?.name).toBe("shop.example.com-0001");
    expect(certificateMention("Certificate for x.io has an unreadable expiry date")?.after).toBe(" has an unreadable expiry date");
    expect(certificateMention("Low disk space: 0.5GB free")).toBeNull();
  });
});

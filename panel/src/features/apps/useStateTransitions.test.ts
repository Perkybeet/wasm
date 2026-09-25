import { describe, expect, it } from "vitest";

import { describeTransitions } from "./useStateTransitions";

describe("describeTransitions", () => {
  const before = new Map([
    ["picconia.com", "Running"],
    ["cittek.es", "Running"],
  ]);

  it("says nothing when nothing changed", () => {
    expect(
      describeTransitions(before, [
        { domain: "picconia.com", status: "running" },
        { domain: "cittek.es", status: "Running" },
      ]),
    ).toBeNull();
  });

  it("names every app that changed, in one sentence", () => {
    expect(
      describeTransitions(before, [
        { domain: "picconia.com", status: "deploying" },
        { domain: "cittek.es", status: "stopped" },
      ]),
    ).toEqual({ message: "picconia.com: Deploying. cittek.es: Stopped.", failed: false });
  });

  it("flags a failure so it is said assertively", () => {
    expect(describeTransitions(before, [{ domain: "picconia.com", status: "failed" }])?.failed).toBe(true);
  });

  it("does not count an app that just appeared", () => {
    expect(describeTransitions(before, [{ domain: "new.example.com", status: "running" }])).toBeNull();
  });
});

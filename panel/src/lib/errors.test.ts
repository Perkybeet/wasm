import { describe, expect, it } from "vitest";

import { describeError } from "./errors";

describe("describeError", () => {
  it("keeps the API's hint and detail apart", () => {
    expect(describeError({ error: "unit_failed", detail: "Job failed.", hint: "Check the logs." })).toEqual({
      hint: "Check the logs.",
      detail: "Job failed.",
    });
  });

  it("falls back to an Error's message, verbatim", () => {
    expect(describeError(new Error("nginx: [emerg] unknown directive"))).toEqual({
      hint: null,
      detail: "nginx: [emerg] unknown directive",
    });
  });

  it("stringifies anything else", () => {
    expect(describeError("boom")).toEqual({ hint: null, detail: "boom" });
  });
});

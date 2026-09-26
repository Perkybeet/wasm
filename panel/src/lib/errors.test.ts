import { describe, expect, it } from "vitest";

import { describeError } from "./errors";

describe("describeError", () => {
  it("keeps the API's hint and detail apart", () => {
    expect(describeError({ error: "unit_failed", detail: "Job failed.", hint: "Check the logs." })).toEqual({
      hint: "Check the logs.",
      detail: "Job failed.",
      output: null,
    });
  });

  it("falls back to an Error's message, verbatim", () => {
    expect(describeError(new Error("nginx: [emerg] unknown directive"))).toEqual({
      hint: null,
      detail: "nginx: [emerg] unknown directive",
      output: null,
    });
  });

  it("stringifies anything else", () => {
    expect(describeError("boom")).toEqual({ hint: null, detail: "boom", output: null });
  });

  it("carries a failing tool's own output apart from the one-line detail", () => {
    expect(
      describeError({
        error: "query_failed",
        detail: "ERROR: syntax error at or near \"SELCT\"",
        hint: null,
        output: 'psql:query.sql:1: ERROR:  syntax error at or near "SELCT"\nLINE 1: SELCT * FROM apps;\n        ^',
      }),
    ).toEqual({
      hint: null,
      detail: 'ERROR: syntax error at or near "SELCT"',
      output: 'psql:query.sql:1: ERROR:  syntax error at or near "SELCT"\nLINE 1: SELCT * FROM apps;\n        ^',
    });
  });

  it("has no output when the error does not carry one", () => {
    expect(describeError({ detail: "Not found", output: null }).output).toBeNull();
    expect(describeError({ detail: "Not found" }).output).toBeNull();
  });
});

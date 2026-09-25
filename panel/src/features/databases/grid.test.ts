import { describe, expect, it } from "vitest";

import { fieldDelimiter, parseQueryGrid } from "./grid";

describe("parseQueryGrid", () => {
  it("splits PostgreSQL's pipe-separated, header-less output", () => {
    const output = "1024|maria@example.com|129.90\n1025|jon@example.com|54.00\n";
    expect(parseQueryGrid("postgresql", output)).toEqual({
      columns: ["Column 1", "Column 2", "Column 3"],
      rows: [
        ["1024", "maria@example.com", "129.90"],
        ["1025", "jon@example.com", "54.00"],
      ],
    });
  });

  it("splits MySQL's tab-separated output", () => {
    const output = "1024\tmaria@example.com\t129.90\n";
    expect(parseQueryGrid("mysql", output)).toEqual({
      columns: ["Column 1", "Column 2", "Column 3"],
      rows: [["1024", "maria@example.com", "129.90"]],
    });
  });

  it("pads ragged rows to the widest row seen", () => {
    const output = "1|a|b\n2|c\n";
    expect(parseQueryGrid("postgresql", output).rows).toEqual([
      ["1", "a", "b"],
      ["2", "c", ""],
    ]);
  });

  it("is empty for empty output, such as a statement with no rows", () => {
    expect(parseQueryGrid("postgresql", "")).toEqual({ columns: [], rows: [] });
    expect(parseQueryGrid("postgresql", "\n\n")).toEqual({ columns: [], rows: [] });
  });

  it("falls back to tab-separated for an engine it does not special-case", () => {
    expect(fieldDelimiter("mongodb").test("a\tb")).toBe(true);
  });
});

import { describe, expect, it } from "vitest";

import { contrastRatio, parseHex, readThemeTokens } from "./contrast";

describe("contrast", () => {
  it("measures the extremes of the WCAG scale", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#777777", "#777777")).toBe(1);
  });

  it("is symmetric", () => {
    expect(contrastRatio("#6a45d5", "#ffffff")).toBeCloseTo(contrastRatio("#ffffff", "#6a45d5"), 10);
  });

  it("matches a published reference pair", () => {
    // #767676 on white is the classic lightest grey that passes AA: 4.54:1.
    expect(contrastRatio("#767676", "#ffffff")).toBeCloseTo(4.54, 2);
  });

  it("parses short and long hex and rejects anything else", () => {
    expect(parseHex("#fff")).toEqual([255, 255, 255]);
    expect(parseHex("#1a2B3c")).toEqual([26, 43, 60]);
    expect(() => parseHex("rgb(0 0 0)")).toThrow("Not a hex colour");
  });

  it("reads light-dark pairs and plain hex from the :root block only", () => {
    const tokens = readThemeTokens(`
      :root {
        --bg: light-dark(#ffffff, #000000);
        --on: #ffffff;
        --shadow: 0 1px 2px rgb(0 0 0 / 0.1);
      }
      [data-theme="dark"] { --bg: #123456; }
    `);
    expect(tokens.light).toEqual({ bg: "#ffffff", on: "#ffffff" });
    expect(tokens.dark).toEqual({ bg: "#000000", on: "#ffffff" });
  });
});

import { describe, expect, it } from "vitest";

import { alignSeries, latest, resolutionWords } from "./series";

describe("alignSeries", () => {
  it("puts every series on one ascending time axis", () => {
    const rx = [
      [100, 1],
      [102, 2],
    ] as const;
    const tx = [
      [100, 5],
      [102, 6],
    ] as const;
    expect(alignSeries([rx, tx])).toEqual({ timestamps: [100, 102], values: [[1, 2], [5, 6]] });
  });

  it("leaves a gap where a series has no sample, rather than inventing one", () => {
    const rx = [
      [100, 1],
      [104, 3],
    ] as const;
    const tx = [
      [102, 6],
      [104, 7],
    ] as const;
    expect(alignSeries([rx, tx])).toEqual({
      timestamps: [100, 102, 104],
      values: [
        [1, null, 3],
        [null, 6, 7],
      ],
    });
  });

  it("is empty for no points", () => {
    expect(alignSeries([[], []])).toEqual({ timestamps: [], values: [[], []] });
  });
});

describe("latest", () => {
  it("reads the newest value", () => {
    expect(latest([[1, 4], [2, 9]])).toBe(9);
    expect(latest([])).toBeNull();
    expect(latest(undefined)).toBeNull();
  });
});

describe("resolutionWords", () => {
  it("says how far apart the points of a read are, and nothing for raw samples", () => {
    expect(resolutionWords("hour")).toBe("hourly averages");
    expect(resolutionWords("minute")).toBe("minute averages");
    expect(resolutionWords("raw")).toBeNull();
    expect(resolutionWords(undefined)).toBeNull();
  });
});

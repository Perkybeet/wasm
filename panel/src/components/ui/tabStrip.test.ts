import { describe, expect, it } from "vitest";

import { scrollToCentre, stripOverflow } from "./tabStrip";

function rect(left: number, width: number): DOMRect {
  return { left, width, right: left + width, top: 0, bottom: 40, height: 40, x: left, y: 0, toJSON: () => ({}) };
}

describe("stripOverflow", () => {
  it("says which ends hide tabs", () => {
    expect(stripOverflow(0, 390, 390)).toBe("none");
    expect(stripOverflow(0, 390, 700)).toBe("end");
    expect(stripOverflow(310, 390, 700)).toBe("start");
    expect(stripOverflow(120, 390, 700)).toBe("both");
  });

  it("ignores sub-pixel remainders", () => {
    expect(stripOverflow(1, 390, 391.5)).toBe("none");
  });
});

describe("scrollToCentre", () => {
  const strip = rect(0, 390);

  it("leaves a tab that is fully in view where it is", () => {
    expect(scrollToCentre(strip, rect(100, 80), 0)).toBeNull();
  });

  it("brings a tab off to the right to the middle", () => {
    // The tab starts 500px in; centred it starts at (390 - 80) / 2 = 155.
    expect(scrollToCentre(strip, rect(500, 80), 0)).toBe(345);
  });

  it("brings a tab cut off on the left back, never past the start", () => {
    expect(scrollToCentre(strip, rect(-40, 80), 60)).toBe(0);
    expect(scrollToCentre(strip, rect(-40, 80), 300)).toBe(105);
  });
});

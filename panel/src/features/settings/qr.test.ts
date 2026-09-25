import { describe, expect, it } from "vitest";

import { qrPath } from "./qr";

/** The dark modules a path draws, as "row,col" keys, from its `M x y h n v1 h-n z` runs. */
function darkModules(d: string): Set<string> {
  const dark = new Set<string>();
  for (const [, x, y, width] of d.matchAll(/M(\d+) (\d+)h(\d+)v1h-\d+z/g)) {
    for (let i = 0; i < Number(width); i += 1) dark.add(`${String(Number(y))},${String(Number(x) + i)}`);
  }
  return dark;
}

/** The 7x7 finder pattern: a dark ring, a light ring, a dark 3x3 centre. */
function hasFinder(dark: Set<string>, top: number, left: number): boolean {
  for (let r = 0; r < 7; r += 1) {
    for (let c = 0; c < 7; c += 1) {
      const ring = r === 0 || r === 6 || c === 0 || c === 6;
      const centre = r >= 2 && r <= 4 && c >= 2 && c <= 4;
      if (dark.has(`${String(top + r)},${String(left + c)}`) !== (ring || centre)) return false;
    }
  }
  return true;
}

const URI = "otpauth://totp/WASM%3Aweb-01?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=WASM&algorithm=SHA1&digits=6&period=30";

describe("qrPath", () => {
  it("encodes an otpauth URI into a code with the three finder patterns", () => {
    const { size, d } = qrPath(URI);
    // A QR code of version v is 17 + 4v modules a side.
    expect((size - 17) % 4).toBe(0);
    expect(size).toBeGreaterThanOrEqual(21);
    const dark = darkModules(d);
    expect(hasFinder(dark, 0, 0)).toBe(true);
    expect(hasFinder(dark, 0, size - 7)).toBe(true);
    expect(hasFinder(dark, size - 7, 0)).toBe(true);
    expect(hasFinder(dark, size - 7, size - 7)).toBe(false);
  });

  it("is a path of rectangles only, never markup", () => {
    const { d } = qrPath(URI);
    expect(d).toMatch(/^(M\d+ \d+h\d+v1h-\d+z)+$/);
  });

  it("is the same code for the same text, and a different one for different text", () => {
    expect(qrPath(URI)).toEqual(qrPath(URI));
    expect(qrPath(URI).d).not.toBe(qrPath(URI.replace("web-01", "web-02")).d);
  });
});

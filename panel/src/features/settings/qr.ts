/**
 * A QR code as one SVG path, for the two-factor enrolment.
 *
 * `qrcode-generator` encodes the data (pure JavaScript, no DOM); this turns its module matrix
 * into a path string that React renders as an attribute of a <path>. Nothing here produces
 * markup: the library's own createSvgTag/createImgTag return HTML strings, which would have to
 * be injected with innerHTML - a Trusted Types sink the console never uses.
 */

import qrcode from "qrcode-generator";

/** Modules of light margin a scanner needs around the code (ISO/IEC 18004). */
export const QUIET_ZONE = 4;

export interface QrPath {
  /** Modules per side, without the quiet zone. */
  size: number;
  /** The dark modules as one path, in module units, with (0, 0) at the top-left module. */
  d: string;
}

/**
 * Encodes text as a QR code and draws its dark modules as one path. Runs of dark modules in a
 * row become one rectangle, which keeps the path short and the edges crisp.
 *
 * Error correction M: an otpauth URI fits comfortably, and a phone camera tolerates glare and
 * a little blur on a screen.
 */
export function qrPath(text: string): QrPath {
  const code = qrcode(0, "M");
  code.addData(text, "Byte");
  code.make();
  const size = code.getModuleCount();
  const parts: string[] = [];
  for (let row = 0; row < size; row += 1) {
    let col = 0;
    while (col < size) {
      if (!code.isDark(row, col)) {
        col += 1;
        continue;
      }
      const start = col;
      while (col < size && code.isDark(row, col)) col += 1;
      parts.push(`M${String(start)} ${String(row)}h${String(col - start)}v1h-${String(col - start)}z`);
    }
  }
  return { size, d: parts.join("") };
}

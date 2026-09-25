import { useMemo } from "react";

import { cx } from "../../lib/cx";
import { QUIET_ZONE, qrPath } from "./qr";

export interface QrCodeProps {
  /** The text to encode, such as an otpauth:// URI. */
  value: string;
  /** What the code is, for assistive technology. The value itself is offered as text nearby. */
  label: string;
  /** Rough width and height in CSS pixels; rounded so every module is a whole number of pixels. */
  size?: number;
  className?: string;
}

/**
 * A QR code drawn as React SVG: one <path> whose `d` is computed here, never markup injected
 * into the page, so it needs nothing the Content Security Policy or Trusted Types forbid.
 */
export function QrCode({ value, label, size = 176, className }: QrCodeProps) {
  const { size: modules, d } = useMemo(() => qrPath(value), [value]);
  const span = modules + QUIET_ZONE * 2;
  // Whole pixels per module: a fractional scale draws modules of uneven width, which some
  // cameras read badly.
  const pixels = span * Math.max(2, Math.round(size / span));
  return (
    <svg
      role="img"
      aria-label={label}
      viewBox={`${String(-QUIET_ZONE)} ${String(-QUIET_ZONE)} ${String(span)} ${String(span)}`}
      width={pixels}
      height={pixels}
      shapeRendering="crispEdges"
      data-modules={modules}
      className={cx("block shrink-0 rounded-control border border-border", className)}
    >
      {/* Dark on light in both themes, not tokens: an authenticator's camera expects exactly
          that, and an inverted code fails in several apps. */}
      <rect x={-QUIET_ZONE} y={-QUIET_ZONE} width={span} height={span} fill="white" />
      <path d={d} fill="black" />
    </svg>
  );
}

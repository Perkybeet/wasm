import { useId } from "react";

import { cx } from "../../lib/cx";

// A gear (the server) with an arrow rising out of its hub (the deploy). Drawn on a 32 unit
// grid so it stays crisp from a 16px favicon up.
const GEAR =
  "M12.74 4.24L13.10 1.08A15.20 15.20 0 0 1 18.90 1.08L19.26 4.24A12.20 12.20 0 0 1 22.01 5.38L24.50 3.40A15.20 15.20 0 0 1 28.60 7.50L26.62 9.99A12.20 12.20 0 0 1 27.76 12.74L30.92 13.10A15.20 15.20 0 0 1 30.92 18.90L27.76 19.26A12.20 12.20 0 0 1 26.62 22.01L28.60 24.50A15.20 15.20 0 0 1 24.50 28.60L22.01 26.62A12.20 12.20 0 0 1 19.26 27.76L18.90 30.92A15.20 15.20 0 0 1 13.10 30.92L12.74 27.76A12.20 12.20 0 0 1 9.99 26.62L7.50 28.60A15.20 15.20 0 0 1 3.40 24.50L5.38 22.01A12.20 12.20 0 0 1 4.24 19.26L1.08 18.90A15.20 15.20 0 0 1 1.08 13.10L4.24 12.74A12.20 12.20 0 0 1 5.38 9.99L3.40 7.50A15.20 15.20 0 0 1 7.50 3.40L9.99 5.38A12.20 12.20 0 0 1 12.74 4.24ZM7.60 16.00a8.40 8.40 0 1 0 16.80 0a8.40 8.40 0 1 0 -16.80 0Z";
const ARROW = "M16 8.4L21 14.8H17.7V25.2H14.3V14.8H11Z";

export interface LogoMarkProps {
  size?: number;
  /** Accessible name when the mark stands alone. Omit when a visible wordmark sits next to it. */
  title?: string;
  className?: string;
}

/** The mark. The brand gradient lives here and nowhere else in the interface. */
export function LogoMark({ size = 24, title, className }: LogoMarkProps) {
  // useId output contains characters that are not safe inside url(#...).
  const gradient = `logo-${useId().replace(/[^a-zA-Z0-9_-]/g, "")}`;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      className={cx("shrink-0", className)}
      {...(title !== undefined ? { role: "img", "aria-label": title } : { "aria-hidden": true })}
    >
      <defs>
        <linearGradient id={gradient} x1="3" y1="29" x2="29" y2="3" gradientUnits="userSpaceOnUse">
          <stop offset="0" style={{ stopColor: "var(--brand-violet)" }} />
          <stop offset="0.5" style={{ stopColor: "var(--brand-blue)" }} />
          <stop offset="1" style={{ stopColor: "var(--brand-green)" }} />
        </linearGradient>
      </defs>
      <path fill={`url(#${gradient})`} fillRule="evenodd" d={GEAR} />
      <path fill={`url(#${gradient})`} d={ARROW} />
    </svg>
  );
}

export interface LogoProps {
  size?: "sm" | "md" | "lg";
  /** Adds a product line after the wordmark, such as "Console". */
  product?: string;
  className?: string;
}

const SIZES = {
  sm: { mark: 20, text: "text-14", gap: "gap-2" },
  md: { mark: 24, text: "text-16", gap: "gap-2.5" },
  lg: { mark: 40, text: "text-24", gap: "gap-3" },
} as const;

/** Mark and wordmark. The wordmark is set wide in Mona Sans, in the text colour. */
export function Logo({ size = "md", product, className }: LogoProps) {
  const spec = SIZES[size];
  return (
    <span className={cx("inline-flex items-center", spec.gap, className)}>
      <LogoMark size={spec.mark} />
      <span className={cx("leading-none font-bold tracking-[0.02em] text-fg [font-stretch:125%]", spec.text)}>WASM</span>
      {product !== undefined ? (
        <span className={cx("leading-none font-medium text-fg-muted", spec.text)}>{product}</span>
      ) : null}
    </span>
  );
}

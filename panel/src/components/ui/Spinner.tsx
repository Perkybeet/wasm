import { cx } from "../../lib/cx";

export interface SpinnerProps {
  /** Pixel size of the square. */
  size?: 12 | 14 | 16 | 20 | 24;
  /**
   * Accessible name. Omit it when the spinner sits inside something that already says it is
   * busy (a button with aria-busy, a labelled region); the spinner is then decorative.
   */
  label?: string;
  className?: string;
}

/** An indeterminate activity indicator: a quarter arc on a faint track. */
export function Spinner({ size = 16, label, className }: SpinnerProps) {
  const svg = (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
      className={cx("shrink-0 animate-spin", className)}
    >
      <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeOpacity="0.22" strokeWidth="1.5" />
      <path d="M8 1.75A6.25 6.25 0 0 1 14.25 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
  if (label === undefined) return svg;
  return (
    <span role="status" className="inline-flex items-center">
      {svg}
      <span className="sr-only">{label}</span>
    </span>
  );
}

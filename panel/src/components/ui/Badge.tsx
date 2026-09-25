import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export type BadgeTone = "neutral" | "accent" | "ok" | "warn" | "fail";

export interface BadgeProps {
  children: ReactNode;
  /** Neutral unless the badge reports a state. Colour always means something. */
  tone?: BadgeTone;
  /** Set the content as a system value (runtime version, branch). */
  mono?: boolean;
  className?: string;
}

const TONES: Record<BadgeTone, string> = {
  neutral: "border-border bg-bg-sunken text-fg-muted",
  accent: "border-transparent bg-accent-soft text-accent-fg",
  ok: "border-transparent bg-ok-soft text-ok",
  warn: "border-transparent bg-warn-soft text-warn",
  fail: "border-transparent bg-fail-soft text-fail",
};

/** A short attribute: a type, a count, a version. For state, use StatusPill. */
export function Badge({ children, tone = "neutral", mono = false, className }: BadgeProps) {
  return (
    <span
      className={cx(
        "inline-flex h-5 shrink-0 items-center gap-1 rounded-[5px] border px-1.5 text-12 leading-none font-medium whitespace-nowrap",
        mono && "mono font-normal",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

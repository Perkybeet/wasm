import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface MonoProps {
  children: ReactNode;
  tone?: "default" | "muted" | "faint";
  /** Cut with an ellipsis instead of wrapping; the full value stays in the title. */
  truncate?: boolean;
  title?: string;
  className?: string;
}

const TONES = { default: "text-fg", muted: "text-fg-muted", faint: "text-fg-faint" } as const;

/**
 * A system value: a path, port, unit name, commit, ID. Set in JetBrains Mono with tabular,
 * slashed figures and no ligatures, and excluded from page translation.
 */
export function Mono({ children, tone = "default", truncate = false, title, className }: MonoProps) {
  return (
    <span
      translate="no"
      title={title ?? (truncate && typeof children === "string" ? children : undefined)}
      className={cx(
        "mono text-[0.92em]",
        TONES[tone],
        truncate && "inline-block max-w-full truncate align-bottom",
        className,
      )}
    >
      {children}
    </span>
  );
}

import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface KbdProps {
  children: ReactNode;
  /** `inverse` sits on an inverted surface such as a tooltip. */
  tone?: "default" | "inverse";
  className?: string;
}

/** One key of a keyboard shortcut. Render a combination as adjacent keys. */
export function Kbd({ children, tone = "default", className }: KbdProps) {
  return (
    <kbd
      className={cx(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-[4px] px-1 text-12 leading-none font-medium",
        tone === "default"
          ? "border border-border bg-surface text-fg-muted shadow-[inset_0_-1px_0_var(--border)]"
          : "bg-bg/15 text-bg",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

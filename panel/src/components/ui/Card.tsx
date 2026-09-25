import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface CardProps {
  title?: ReactNode;
  description?: ReactNode;
  /** Controls aligned with the title: a button, a menu, a link. */
  actions?: ReactNode;
  footer?: ReactNode;
  children?: ReactNode;
  /** Heading level of the title, to keep the page outline correct. */
  level?: 2 | 3 | 4;
  /** `none` lets tables and lists run edge to edge. */
  padding?: "none" | "md";
  className?: string;
}

/** A bounded surface that groups one subject. Not a decoration: use one only to group. */
export function Card({
  title,
  description,
  actions,
  footer,
  children,
  level = 3,
  padding = "md",
  className,
}: CardProps) {
  const Heading = `h${level}` as const;
  const hasHeader = title !== undefined || actions !== undefined;
  return (
    <section
      className={cx(
        "flex min-w-0 flex-col rounded-card border border-border bg-surface shadow-raised",
        className,
      )}
    >
      {hasHeader ? (
        <header className="flex items-start justify-between gap-4 px-5 pt-4 pb-3">
          <div className="min-w-0">
            {title !== undefined ? <Heading className="text-14 font-semibold text-fg">{title}</Heading> : null}
            {description !== undefined ? <p className="mt-0.5 text-13 text-fg-muted">{description}</p> : null}
          </div>
          {actions !== undefined ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}
      {children !== undefined ? (
        <div
          className={cx(
            "min-w-0 flex-1",
            padding === "md" && (hasHeader ? "px-5 pb-5" : "p-5"),
            padding === "none" && hasHeader && "border-t border-border",
          )}
        >
          {children}
        </div>
      ) : null}
      {footer !== undefined ? (
        <footer className="flex items-center justify-end gap-2 rounded-b-card border-t border-border bg-bg-sunken/60 px-5 py-3">
          {footer}
        </footer>
      ) : null}
    </section>
  );
}

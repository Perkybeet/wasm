import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { CopyButton } from "./CopyButton";

export interface EmptyStateProps {
  /** A lucide icon element, drawn at 20px. */
  icon?: ReactNode;
  title: string;
  /** What this place is for and what to do next. An invitation, not an apology. */
  description?: ReactNode;
  action?: ReactNode;
  /** The CLI command that does the same thing, for operators who live in a terminal. */
  command?: string;
  /** Heading level of the title, to keep the page outline correct: 2 directly under a page h1. */
  level?: 2 | 3 | 4;
  className?: string;
}

/** What a list or page shows before it has anything in it. */
export function EmptyState({ icon, title, description, action, command, level = 3, className }: EmptyStateProps) {
  const Heading = `h${level}` as const;
  return (
    <div
      className={cx(
        "flex flex-col items-center rounded-card border border-dashed border-border px-6 py-12 text-center",
        className,
      )}
    >
      {icon !== undefined ? (
        <div className="mb-4 flex size-10 items-center justify-center rounded-control border border-border bg-surface text-fg-muted shadow-raised [&_svg]:size-5">
          {icon}
        </div>
      ) : null}
      <Heading className="title text-16 text-fg">{title}</Heading>
      {description !== undefined ? (
        <p className="mt-1.5 max-w-[46ch] text-14 text-pretty text-fg-muted">{description}</p>
      ) : null}
      {action !== undefined ? <div className="mt-5 flex flex-wrap justify-center gap-2">{action}</div> : null}
      {command !== undefined ? (
        <div className="mt-5 flex max-w-full items-center gap-1 rounded-control border border-border bg-bg-sunken py-1 pr-1 pl-3">
          <span className="mono truncate text-13 text-fg-muted" translate="no">
            <span aria-hidden="true" className="text-fg-faint select-none">
              ${" "}
            </span>
            {command}
          </span>
          <CopyButton value={command} label="Copy command" />
        </div>
      ) : null}
    </div>
  );
}

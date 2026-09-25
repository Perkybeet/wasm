import { useId } from "react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface DangerZoneProps {
  /** Rows of DangerAction. */
  children: ReactNode;
  title?: string;
  description?: ReactNode;
  /** Heading level of the zone's title; its actions' titles sit one below. */
  level?: 2 | 3;
  className?: string;
}

/**
 * Where the actions that cannot be undone live, apart from everything else and last on the
 * page, each one explaining what it destroys before its button does it.
 */
export function DangerZone({ children, title = "Danger zone", description, level = 2, className }: DangerZoneProps) {
  const headingId = useId();
  const Heading = `h${level}` as const;
  return (
    <section aria-labelledby={headingId} className={cx("flex min-w-0 flex-col gap-4", className)}>
      <header>
        <Heading id={headingId} className="title text-16 text-fg">
          {title}
        </Heading>
        {description !== undefined ? <p className="mt-0.5 text-13 text-fg-muted">{description}</p> : null}
      </header>
      <div className="flex flex-col divide-y divide-fail/20 rounded-card border border-fail/40 bg-surface shadow-raised">
        {children}
      </div>
    </section>
  );
}

export interface DangerActionProps {
  /** What the action does, as a verb phrase: "Delete this application". */
  title: string;
  /** Exactly what is destroyed and what is kept. */
  description: ReactNode;
  /** The button (usually opening a ConfirmDialog), in the danger variant. */
  action: ReactNode;
  /** Heading level of the title: one below the zone's. */
  level?: 3 | 4;
}

/** One irreversible action: what it does, what it takes with it, and the button. */
export function DangerAction({ title, description, action, level = 3 }: DangerActionProps) {
  const Heading = `h${level}` as const;
  return (
    <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0">
        <Heading className="text-14 font-medium text-fg">{title}</Heading>
        <p className="mt-0.5 max-w-[60ch] text-13 text-pretty text-fg-muted">{description}</p>
      </div>
      <div className="shrink-0">{action}</div>
    </div>
  );
}

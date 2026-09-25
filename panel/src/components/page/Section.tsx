import { useId } from "react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface SectionProps {
  title: string;
  /** One sentence on what the section holds, when the title alone does not say it. */
  description?: ReactNode;
  /** Controls for the whole section (a range picker, "View all"), aligned with the title. */
  actions?: ReactNode;
  children: ReactNode;
  /** Heading level: 2 directly under the page's h1, 3 inside a section. */
  level?: 2 | 3;
  /** A count or state shown right after the title ("Needs attention 3"). */
  badge?: ReactNode;
  className?: string;
}

/**
 * One subject of a page: a heading, an optional line of description and the section's
 * actions, then the content 16px below. A landmark region named by its heading, so a screen
 * reader can jump between sections.
 */
export function Section({ title, description, actions, children, level = 2, badge, className }: SectionProps) {
  const headingId = useId();
  const Heading = `h${level}` as const;
  return (
    <section aria-labelledby={headingId} className={cx("flex min-w-0 flex-col gap-4", className)}>
      <header className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Heading id={headingId} className={cx("title text-fg", level === 2 ? "text-16" : "text-14")}>
              {title}
            </Heading>
            {badge}
          </div>
          {description !== undefined ? (
            <p className="mt-0.5 max-w-[68ch] text-13 text-pretty text-fg-muted">{description}</p>
          ) : null}
        </div>
        {actions !== undefined ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}

/**
 * The body of a page: sections 32px apart. Pages use it right after the PageHeader so the
 * rhythm is the same everywhere.
 */
export function Sections({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("flex min-w-0 flex-col gap-8", className)}>{children}</div>;
}

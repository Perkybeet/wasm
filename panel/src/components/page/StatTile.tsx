import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface StatTileProps {
  /** What the number is, in sentence case: "Current release". */
  label: string;
  /** The reading. A string or number is set large; an element (a pill) as it is. */
  value: ReactNode;
  /** One line of context under the value: when, of what, compared with what. */
  detail?: ReactNode;
  /** Set the value as a system value (a commit, a port) in mono. */
  mono?: boolean;
  className?: string;
}

/**
 * One headline fact with a line of context. Tiles sit side by side in a row that answers "how
 * is this doing" before the details below it do. The value uses proportional figures; mono
 * is for identifiers, not for quantities.
 */
export function StatTile({ label, value, detail, mono = false, className }: StatTileProps) {
  const primitive = typeof value === "string" || typeof value === "number";
  return (
    <div className={cx("flex min-w-0 flex-col gap-1.5 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised", className)}>
      <span className="truncate text-12 text-fg-muted">{label}</span>
      <div className="flex min-h-7 min-w-0 items-center">
        {primitive ? (
          <span
            translate={mono ? "no" : undefined}
            className={cx("truncate text-fg", mono ? "mono text-16 font-medium" : "title text-18")}
          >
            {value}
          </span>
        ) : (
          value
        )}
      </div>
      {detail !== undefined ? <div className="truncate text-12 text-fg-faint">{detail}</div> : null}
    </div>
  );
}

import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { CopyButton } from "../ui/CopyButton";
import { Skeleton } from "../ui/Skeleton";

export interface KeyValueItem {
  label: string;
  /** The value. Strings and numbers are system values: set in mono and copyable. */
  value: ReactNode;
  /**
   * What the copy button puts on the clipboard. Defaults to the value when it is a string
   * or a number; `false` offers no copy (a state, a relative time).
   */
  copy?: string | false;
  /** Overrides the mono default: true for a system value built from elements, false for prose. */
  mono?: boolean;
  /** A short note under the value, in the interface's voice ("set by MemoryMax"). */
  hint?: ReactNode;
}

export interface KeyValueListProps {
  items: readonly KeyValueItem[];
  /** Shown for a value that is null, undefined or empty. */
  empty?: string;
  className?: string;
}

function isBlank(value: ReactNode): boolean {
  return value === null || value === undefined || value === "" || value === false;
}

/**
 * Facts about one thing, one per row: a dense definition list. System values are set in mono
 * and truncated to one line with the full text on hover; the copy button appears on hover and
 * on keyboard focus (and always, on touch screens).
 */
export function KeyValueList({ items, empty = "Not set", className }: KeyValueListProps) {
  return (
    <dl className={cx("flex min-w-0 flex-col divide-y divide-border", className)}>
      {items.map((item) => {
        const blank = isBlank(item.value);
        const text = typeof item.value === "string" || typeof item.value === "number" ? String(item.value) : null;
        const mono = item.mono ?? text !== null;
        const copy = item.copy ?? (text !== null && !blank ? text : false);
        return (
          <div
            key={item.label}
            className="group grid min-h-10 grid-cols-[minmax(6.5rem,30%)_minmax(0,1fr)] items-center gap-x-4 py-1.5"
          >
            <dt className="text-13 text-fg-muted">{item.label}</dt>
            <dd className="flex min-w-0 flex-col">
              <div className="flex min-w-0 items-center gap-1">
                {blank ? (
                  <span className="text-13 text-fg-faint">{empty}</span>
                ) : (
                  <span
                    translate={mono ? "no" : undefined}
                    title={text ?? undefined}
                    className={cx("min-w-0 truncate text-13 text-fg", mono && "mono text-12")}
                  >
                    {item.value}
                  </span>
                )}
                {copy !== false ? (
                  <CopyButton
                    value={copy}
                    label={`Copy ${item.label.toLowerCase()}`}
                    className="-my-1 opacity-0 transition-opacity duration-(--duration-fast) group-hover:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100"
                  />
                ) : null}
              </div>
              {item.hint !== undefined ? <span className="text-12 text-fg-faint">{item.hint}</span> : null}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

/** The loading shape of a KeyValueList: the same rows, the values still to come. */
export function KeyValueListSkeleton({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div aria-hidden="true" className={cx("flex flex-col divide-y divide-border", className)}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="grid min-h-10 grid-cols-[minmax(6.5rem,30%)_minmax(0,1fr)] items-center gap-x-4 py-1.5">
          <Skeleton className="h-3 w-20" />
          <Skeleton className={cx("h-3", i % 2 === 0 ? "w-32" : "w-24")} />
        </div>
      ))}
    </div>
  );
}

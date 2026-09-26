import { cx } from "../../lib/cx";

export interface SkeletonProps {
  /** Size with utilities: `h-4 w-32`. Defaults to a line of body text. */
  className?: string;
}

/**
 * A placeholder with the shape of content that is loading. It is decorative: the region that
 * contains it carries aria-busy and says what is loading.
 */
export function Skeleton({ className }: SkeletonProps) {
  return (
    <span
      aria-hidden="true"
      className={cx("block animate-breathe rounded-[4px] bg-surface-active", sizeDefaults(className), className)}
    />
  );
}

/**
 * The line-of-text size, for whichever dimension the caller did not set. Both utilities in one
 * class list do not "override" each other: Tailwind's stylesheet order decides, so `h-3.5` used
 * to beat a caller's `h-3` and `w-full` every fixed width, and placeholders rendered taller and
 * wider than the content they stand for - a layout shift when it arrived.
 */
function sizeDefaults(className: string | undefined): string {
  const classes = (className ?? "").split(/\s+/);
  const sets = (prefixes: readonly string[]) =>
    classes.some((name) => prefixes.some((prefix) => name.replace(/^[a-z0-9-]+:/, "").startsWith(prefix)));
  const height = sets(["h-", "size-"]) ? "" : "h-3.5";
  const width = sets(["w-", "size-"]) ? "" : "w-full";
  return cx(height, width);
}

/** Lines of text, the last one shorter, the way a paragraph ends. */
export function SkeletonText({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <span aria-hidden="true" className={cx("flex flex-col gap-2", className)}>
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className={i === lines - 1 && lines > 1 ? "w-3/5" : "w-full"} />
      ))}
    </span>
  );
}

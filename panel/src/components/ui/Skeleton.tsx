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
      className={cx("block h-3.5 w-full animate-breathe rounded-[4px] bg-surface-active", className)}
    />
  );
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

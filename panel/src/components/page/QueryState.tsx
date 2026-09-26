import { CircleAlert, RotateCw } from "lucide-react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { describeError } from "../../lib/errors";
import { Button } from "../ui/Button";
import { SystemOutput } from "../ui/SystemOutput";

export interface ErrorBlockProps {
  error: unknown;
  /** What failed, in the interface's words: "Could not load applications". */
  title: string;
  /** The fix when the backend did not send one. The backend's own hint always wins. */
  hint?: string;
  onRetry?: () => void;
  retrying?: boolean;
  /**
   * Announce it: for the outcome of something the operator just did. Leave off for a section
   * that failed to load, or a machine that is down would shout from every section at once.
   */
  live?: boolean;
  compact?: boolean;
  className?: string;
}

/**
 * A failure, the way the console always shows one: what failed, the fix above, and the
 * system's own words below in mono, verbatim, never paraphrased.
 */
export function ErrorBlock({ error, title, hint, onRetry, retrying = false, live = false, compact = false, className }: ErrorBlockProps) {
  const described = describeError(error);
  const fix = described.hint ?? hint;
  // A failing tool (psql, git, nginx) prints its own report on top of the one-line detail;
  // show both unless they are the same words twice.
  const output =
    described.output !== null && described.output.trim() !== "" && described.output.trim() !== described.detail.trim()
      ? described.output
      : null;
  return (
    <div
      {...(live ? { role: "alert" } : {})}
      className={cx(
        "flex min-w-0 flex-col gap-2 rounded-card border border-fail/30 bg-fail-soft/50",
        compact ? "p-3" : "p-4",
        className,
      )}
    >
      <div className="flex items-start gap-2">
        <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-fail" />
        <div className="flex min-w-0 flex-col gap-1">
          <p className="text-13 font-medium text-fg">{title}</p>
          {fix !== undefined ? <p className="text-13 text-pretty text-fg-muted">{fix}</p> : null}
        </div>
      </div>
      <SystemOutput label={`${title}: what the system said`} className="rounded-control border border-border bg-surface px-3 py-2">
        {described.detail}
      </SystemOutput>
      {output !== null ? (
        <SystemOutput
          label={`${title}: the command's own output`}
          maxHeight="max-h-48"
          className="rounded-control border border-border bg-surface px-3 py-2"
        >
          {output}
        </SystemOutput>
      ) : null}
      {onRetry !== undefined ? (
        <div>
          <Button size="sm" icon={<RotateCw aria-hidden="true" />} loading={retrying} onClick={onRetry}>
            Try again
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** The parts of a TanStack Query result this reads, so a test can pass a plain object. */
export interface QueryLike<T> {
  data: T | undefined;
  error: unknown;
  isPending: boolean;
  isError: boolean;
  isRefetching?: boolean;
  refetch?: () => unknown;
}

export interface QueryStateProps<T> {
  query: QueryLike<T>;
  /** What is being loaded, completing "Loading ..." and "Could not load ...": "applications". */
  label: string;
  /** The loading shape: skeletons laid out like the content that will replace them. */
  skeleton: ReactNode;
  /** True when the data holds nothing to show. */
  isEmpty?: (data: T) => boolean;
  /** Shown instead of the content when `isEmpty` says so: what this is and how to create one. */
  empty?: ReactNode;
  /** The fix to suggest when the backend's error carries none. */
  errorHint?: string;
  children: (data: T) => ReactNode;
  className?: string;
}

/**
 * One wrapper for the four states of anything loaded: a skeleton shaped like the content, the
 * error verbatim with its fix, the empty state, and the content. Data that loaded once stays
 * on screen when a refresh fails, with the failure noted above it.
 */
export function QueryState<T>({
  query,
  label,
  skeleton,
  isEmpty,
  empty,
  errorHint,
  children,
  className,
}: QueryStateProps<T>) {
  const retry = query.refetch ? () => void query.refetch?.() : undefined;

  if (query.data === undefined) {
    if (query.isError) {
      return (
        <ErrorBlock
          error={query.error}
          title={`Could not load ${label}`}
          {...(errorHint !== undefined ? { hint: errorHint } : {})}
          {...(retry ? { onRetry: retry } : {})}
          retrying={query.isRefetching ?? false}
          {...(className !== undefined ? { className } : {})}
        />
      );
    }
    return (
      <div aria-busy="true" className={className}>
        <span className="sr-only">{`Loading ${label}`}</span>
        {skeleton}
      </div>
    );
  }

  const data = query.data;
  const content = isEmpty?.(data) && empty !== undefined ? empty : children(data);
  if (!query.isError) return className === undefined ? <>{content}</> : <div className={className}>{content}</div>;

  return (
    <div className={cx("flex min-w-0 flex-col gap-3", className)}>
      <ErrorBlock
        compact
        error={query.error}
        title={`Could not refresh ${label}. What follows is the last answer.`}
        {...(errorHint !== undefined ? { hint: errorHint } : {})}
        {...(retry ? { onRetry: retry } : {})}
        retrying={query.isRefetching ?? false}
      />
      {content}
    </div>
  );
}

import { Meter as BaseMeter } from "@base-ui/react/meter";
import { Progress as BaseProgress } from "@base-ui/react/progress";

import { cx } from "../../lib/cx";

export interface ProgressProps {
  /** Percent complete, 0-100; null when the total is not known yet. */
  value: number | null;
  /** What is progressing: "Uploading backup". */
  label: string;
  /** Show the percentage next to the label. */
  showValue?: boolean;
  className?: string;
}

/** How far a task with a known end has got: an upload, a restore, a build step. */
export function Progress({ value, label, showValue = true, className }: ProgressProps) {
  return (
    <BaseProgress.Root value={value} className={cx("flex min-w-0 flex-col gap-1.5", className)}>
      <div className="flex items-baseline justify-between gap-3">
        <BaseProgress.Label className="truncate text-13 text-fg">{label}</BaseProgress.Label>
        {showValue && value !== null ? (
          <BaseProgress.Value className="mono text-12 text-fg-muted" />
        ) : null}
      </div>
      <BaseProgress.Track className="relative h-1.5 overflow-hidden rounded-pill bg-surface-active">
        <BaseProgress.Indicator
          className={cx(
            "block h-full rounded-pill bg-accent transition-[width] duration-(--duration-base) ease-out",
            "data-indeterminate:w-2/5 data-indeterminate:animate-indeterminate",
            "motion-reduce:data-indeterminate:w-full motion-reduce:data-indeterminate:opacity-40",
          )}
        />
      </BaseProgress.Track>
    </BaseProgress.Root>
  );
}

export interface MeterProps {
  /** The measured amount, in the same unit as `max`. */
  value: number;
  max?: number;
  label: string;
  /** How the value reads aloud and on screen, e.g. "3.1 of 8 GB". Defaults to a percentage. */
  valueText?: string;
  /** Fractions of max at which the fill turns amber, then red. Usage is a state. */
  thresholds?: { warn: number; fail: number };
  size?: "sm" | "md";
  className?: string;
}

/**
 * A level within a known range: CPU, memory, disk. The fill is neutral until the level is a
 * problem, then takes the state colour, and the value is always printed.
 */
export function Meter({
  value,
  max = 100,
  label,
  valueText,
  thresholds = { warn: 0.75, fail: 0.9 },
  size = "md",
  className,
}: MeterProps) {
  const ratio = max > 0 ? value / max : 0;
  const level = ratio >= thresholds.fail ? "fail" : ratio >= thresholds.warn ? "warn" : "normal";
  const fill = { normal: "bg-fg-muted", warn: "bg-warn", fail: "bg-fail" }[level];
  const track = { normal: "bg-surface-active", warn: "bg-warn-soft", fail: "bg-fail-soft" }[level];
  const text = valueText ?? `${String(Math.round(ratio * 100))}%`;
  return (
    <BaseMeter.Root
      value={value}
      max={max}
      aria-valuetext={text}
      className={cx("flex min-w-0 flex-col", size === "sm" ? "gap-1" : "gap-1.5", className)}
    >
      <div className="flex items-baseline justify-between gap-3">
        <BaseMeter.Label className={cx("truncate text-fg-muted", size === "sm" ? "text-12" : "text-13")}>
          {label}
        </BaseMeter.Label>
        <span className={cx("mono text-fg", size === "sm" ? "text-12" : "text-13")}>{text}</span>
      </div>
      <BaseMeter.Track
        data-level={level}
        className={cx("relative overflow-hidden rounded-pill", track, size === "sm" ? "h-1" : "h-1.5")}
      >
        <BaseMeter.Indicator className={cx("block h-full rounded-pill transition-[width] duration-(--duration-base) ease-out", fill)} />
      </BaseMeter.Track>
    </BaseMeter.Root>
  );
}

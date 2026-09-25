import { Radio } from "@base-ui/react/radio";
import { RadioGroup } from "@base-ui/react/radio-group";

import { cx } from "../../lib/cx";

export interface SegmentedOption<V extends string> {
  value: V;
  label: string;
}

export interface SegmentedControlProps<V extends string> {
  /** Names the choice for assistive technology: "Time range". */
  label: string;
  options: readonly SegmentedOption<V>[];
  value: V;
  onValueChange: (value: V) => void;
  className?: string;
}

/**
 * One of a few mutually exclusive views of the same thing (a chart's time range). A radio
 * group: one tab stop, arrow keys move the choice.
 */
export function SegmentedControl<V extends string>({ label, options, value, onValueChange, className }: SegmentedControlProps<V>) {
  return (
    <RadioGroup<V>
      aria-label={label}
      value={value}
      onValueChange={(next: V) => {
        onValueChange(next);
      }}
      className={cx("inline-flex shrink-0 items-center gap-0.5 rounded-control border border-border bg-bg-sunken p-0.5", className)}
    >
      {options.map((option) => (
        <Radio.Root
          key={option.value}
          value={option.value}
          className={cx(
            "inline-flex h-6 min-w-9 cursor-pointer items-center justify-center rounded-[4px] px-2 text-12 font-medium text-fg-muted select-none",
            "transition-[background-color,color] duration-(--duration-fast) ease-out hover:text-fg",
            "data-checked:bg-surface data-checked:text-fg data-checked:shadow-raised",
            "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus",
          )}
        >
          {option.label}
        </Radio.Root>
      ))}
    </RadioGroup>
  );
}

import { Checkbox as BaseCheckbox } from "@base-ui/react/checkbox";
import { Check, Minus } from "lucide-react";
import { useId } from "react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface CheckboxProps {
  label?: ReactNode;
  /** A second line under the label: the consequence of ticking it. */
  description?: ReactNode;
  /** Required when there is no visible label. */
  "aria-label"?: string;
  checked?: boolean;
  defaultChecked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
  /** Some, not all, of a group is ticked. */
  indeterminate?: boolean;
  disabled?: boolean;
  name?: string;
  className?: string;
}

/** An independent yes or no that takes effect when the form is saved. For instant effect, use Switch. */
export function Checkbox({
  label,
  description,
  "aria-label": ariaLabel,
  checked,
  defaultChecked,
  onCheckedChange,
  indeterminate = false,
  disabled = false,
  name,
  className,
}: CheckboxProps) {
  const labelId = useId();
  const descriptionId = useId();
  const described = label !== undefined && description !== undefined;
  const box = (
    <BaseCheckbox.Root
      {...(checked !== undefined ? { checked } : {})}
      {...(defaultChecked !== undefined ? { defaultChecked } : {})}
      {...(onCheckedChange ? { onCheckedChange: (next: boolean) => onCheckedChange(next) } : {})}
      {...(ariaLabel !== undefined ? { "aria-label": ariaLabel } : {})}
      {...(described ? { "aria-labelledby": labelId, "aria-describedby": descriptionId } : {})}
      {...(name !== undefined ? { name } : {})}
      indeterminate={indeterminate}
      disabled={disabled}
      className={cx(
        "mt-0.5 flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-[4px] border border-border-strong bg-surface text-on-accent",
        "transition-[background-color,border-color] duration-(--duration-fast) ease-out",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "data-checked:border-accent data-checked:bg-accent data-indeterminate:border-accent data-indeterminate:bg-accent",
        "data-disabled:cursor-not-allowed data-disabled:opacity-50",
      )}
    >
      <BaseCheckbox.Indicator className="flex data-unchecked:hidden">
        {indeterminate ? (
          <Minus aria-hidden="true" className="size-3" strokeWidth={3} />
        ) : (
          <Check aria-hidden="true" className="size-3" strokeWidth={3} />
        )}
      </BaseCheckbox.Indicator>
    </BaseCheckbox.Root>
  );

  if (label === undefined) return box;
  return (
    // The Base UI root is a span with role="checkbox"; an enclosing label names it and makes
    // the whole row a click target.
    <label
      className={cx(
        "flex cursor-pointer items-start gap-2.5 text-14 text-fg",
        disabled && "cursor-not-allowed text-fg-muted",
        className,
      )}
    >
      {box}
      <span className="flex flex-col">
        <span id={labelId}>{label}</span>
        {/* The enclosing label would read both lines as the name; the second is the
            consequence of ticking, so it is named apart and read as the description. */}
        {description !== undefined ? (
          <span id={descriptionId} className="text-13 text-fg-muted">
            {description}
          </span>
        ) : null}
      </span>
    </label>
  );
}

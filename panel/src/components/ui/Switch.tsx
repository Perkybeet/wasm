import { Switch as BaseSwitch } from "@base-ui/react/switch";
import { useId } from "react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface SwitchProps {
  label?: ReactNode;
  description?: ReactNode;
  /** Required when there is no visible label. */
  "aria-label"?: string;
  checked?: boolean;
  defaultChecked?: boolean;
  onCheckedChange?: (checked: boolean) => void;
  disabled?: boolean;
  name?: string;
  className?: string;
}

/** A setting that takes effect immediately. For a choice saved with a form, use Checkbox. */
export function Switch({
  label,
  description,
  "aria-label": ariaLabel,
  checked,
  defaultChecked,
  onCheckedChange,
  disabled = false,
  name,
  className,
}: SwitchProps) {
  const labelId = useId();
  const descriptionId = useId();
  const described = label !== undefined && description !== undefined;
  const control = (
    <BaseSwitch.Root
      {...(checked !== undefined ? { checked } : {})}
      {...(defaultChecked !== undefined ? { defaultChecked } : {})}
      {...(onCheckedChange ? { onCheckedChange: (next: boolean) => onCheckedChange(next) } : {})}
      {...(ariaLabel !== undefined ? { "aria-label": ariaLabel } : {})}
      {...(described ? { "aria-labelledby": labelId, "aria-describedby": descriptionId } : {})}
      {...(name !== undefined ? { name } : {})}
      disabled={disabled}
      className={cx(
        "relative inline-flex h-[18px] w-8 shrink-0 cursor-pointer items-center rounded-pill border border-transparent bg-border-strong p-px",
        "transition-colors duration-(--duration-base) ease-out",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "data-checked:bg-accent data-disabled:cursor-not-allowed data-disabled:opacity-50",
      )}
    >
      <BaseSwitch.Thumb
        className={cx(
          "block size-3.5 rounded-pill bg-on-accent shadow-[0_1px_2px_rgb(0_0_0/0.25)]",
          "transition-transform duration-(--duration-base) ease-out data-checked:translate-x-3.5",
        )}
      />
    </BaseSwitch.Root>
  );

  if (label === undefined) return control;
  return (
    <label
      className={cx(
        "flex cursor-pointer items-start justify-between gap-4 text-14 text-fg",
        disabled && "cursor-not-allowed text-fg-muted",
        className,
      )}
    >
      <span className="flex flex-col">
        <span id={labelId}>{label}</span>
        {/* Named apart from the label, as the Checkbox does: read as the description. */}
        {description !== undefined ? (
          <span id={descriptionId} className="text-13 text-fg-muted">
            {description}
          </span>
        ) : null}
      </span>
      <span className="mt-0.5 flex">{control}</span>
    </label>
  );
}

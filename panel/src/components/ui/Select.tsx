import { Select as BaseSelect } from "@base-ui/react/select";
import { Check, ChevronsUpDown } from "lucide-react";


import { cx } from "../../lib/cx";
import { CONTROL_FRAME } from "./Input";
import { POPUP_MOTION } from "./Tooltip";

export interface SelectOption<V extends string = string> {
  value: V;
  label: string;
  /** A second line in the list only, such as the command a runtime will use. */
  hint?: string;
  disabled?: boolean;
}

export interface SelectProps<V extends string = string> {
  options: readonly SelectOption<V>[];
  value?: V | null;
  defaultValue?: V | null;
  onValueChange?: (value: V) => void;
  placeholder?: string;
  /** A visible label. Omit it inside a Field, which labels the control instead. */
  label?: string;
  /** Required when there is neither `label` nor an enclosing Field. */
  "aria-label"?: string;
  name?: string;
  disabled?: boolean;
  size?: "sm" | "md";
  mono?: boolean;
  className?: string;
}

/** Choose one value from a short, known list. For long or searchable lists, use a combobox. */
export function Select<V extends string = string>({
  options,
  value,
  defaultValue,
  onValueChange,
  placeholder = "Select",
  label,
  "aria-label": ariaLabel,
  name,
  disabled = false,
  size = "md",
  mono = false,
  className,
}: SelectProps<V>) {
  const items = options.map((o) => ({ value: o.value, label: o.label }));
  return (
    <BaseSelect.Root<V>
      items={items}
      {...(value !== undefined ? { value } : {})}
      {...(defaultValue !== undefined ? { defaultValue } : {})}
      {...(onValueChange
        ? {
            onValueChange: (next: V | null) => {
              if (next !== null) onValueChange(next);
            },
          }
        : {})}
      {...(name !== undefined ? { name } : {})}
      disabled={disabled}
    >
      {label !== undefined ? (
        <BaseSelect.Label className="mb-1.5 block text-13 font-medium text-fg">{label}</BaseSelect.Label>
      ) : null}
      <BaseSelect.Trigger
        {...(ariaLabel !== undefined ? { "aria-label": ariaLabel } : {})}
        className={cx(
          "flex min-w-40 cursor-pointer items-center justify-between gap-2 pr-2 pl-2.5 text-left text-13 select-none",
          CONTROL_FRAME,
          "focus-visible:border-focus focus-visible:outline-2 focus-visible:-outline-offset-1 focus-visible:outline-focus",
          "data-disabled:cursor-not-allowed data-disabled:bg-bg-sunken data-disabled:opacity-60",
          "data-popup-open:border-fg-faint",
          size === "sm" ? "h-7" : "h-8",
          className,
        )}
      >
        <BaseSelect.Value
          placeholder={placeholder}
          className={cx("truncate data-placeholder:text-fg-faint", mono && "mono")}
        />
        <BaseSelect.Icon className="flex text-fg-faint">
          <ChevronsUpDown aria-hidden="true" className="size-3.5" />
        </BaseSelect.Icon>
      </BaseSelect.Trigger>
      <BaseSelect.Portal>
        <BaseSelect.Positioner sideOffset={4} alignItemWithTrigger={false} className="z-50 outline-none">
          <BaseSelect.Popup
            className={cx(
              "max-h-(--available-height) min-w-(--anchor-width) overflow-y-auto rounded-card border border-border bg-surface-raised p-1 text-fg shadow-overlay outline-none scroll-thin",
              POPUP_MOTION,
            )}
          >
            <BaseSelect.List>
              {options.map((option) => (
                <BaseSelect.Item
                  key={option.value}
                  value={option.value}
                  {...(option.disabled ? { disabled: true } : {})}
                  className="grid cursor-pointer grid-cols-[1fr_1rem] items-center gap-3 rounded-control py-1.5 pr-2 pl-2.5 text-13 outline-none select-none data-disabled:cursor-not-allowed data-disabled:opacity-50 data-highlighted:bg-surface-hover"
                >
                  <span className="min-w-0">
                    <BaseSelect.ItemText className={cx("block truncate", mono && "mono")}>{option.label}</BaseSelect.ItemText>
                    {option.hint !== undefined ? (
                      <span className="mono block truncate text-12 text-fg-faint">{option.hint}</span>
                    ) : null}
                  </span>
                  <BaseSelect.ItemIndicator className="flex text-accent-fg">
                    <Check aria-hidden="true" className="size-4" />
                  </BaseSelect.ItemIndicator>
                </BaseSelect.Item>
              ))}
            </BaseSelect.List>
          </BaseSelect.Popup>
        </BaseSelect.Positioner>
      </BaseSelect.Portal>
    </BaseSelect.Root>
  );
}


import { Button as BaseButton } from "@base-ui/react/button";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { Tooltip } from "./Tooltip";

export interface IconButtonProps extends Omit<BaseButton.Props, "className" | "children" | "aria-label"> {
  /** The accessible name, also shown as the tooltip. Say what pressing it does. */
  label: string;
  icon: ReactNode;
  variant?: "ghost" | "secondary";
  size?: "sm" | "md";
  /** For toggles (wrap lines, follow output): renders aria-pressed and a held-down look. */
  pressed?: boolean;
  /** Keys of the equivalent shortcut, shown in the tooltip. */
  shortcut?: readonly string[];
  /** Tooltips help pointer users; turn off where the label is already visible nearby. */
  tooltip?: boolean;
  className?: string;
}

/** A square button whose only content is an icon. The label is mandatory. */
export function IconButton({
  label,
  icon,
  variant = "ghost",
  size = "md",
  pressed,
  shortcut,
  tooltip = true,
  className,
  type = "button",
  ...rest
}: IconButtonProps) {
  const button = (
    <BaseButton
      {...rest}
      type={type}
      aria-label={label}
      aria-pressed={pressed}
      data-pressed={pressed ? "" : undefined}
      className={cx(
        "inline-flex shrink-0 cursor-pointer items-center justify-center rounded-control border text-fg-muted",
        "transition-[background-color,border-color,color] duration-(--duration-fast) ease-out",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "hover:not-data-disabled:bg-surface-hover hover:not-data-disabled:text-fg",
        "data-disabled:cursor-not-allowed data-disabled:opacity-50",
        "data-pressed:bg-surface-active data-pressed:text-fg",
        variant === "ghost" ? "border-transparent" : "border-border bg-surface shadow-raised",
        size === "sm" ? "size-7 [&_svg]:size-3.5" : "size-8 [&_svg]:size-4",
        className,
      )}
    >
      {icon}
    </BaseButton>
  );
  if (!tooltip) return button;
  return (
    <Tooltip content={label} {...(shortcut ? { shortcut } : {})}>
      {button}
    </Tooltip>
  );
}

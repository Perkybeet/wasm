import { Button as BaseButton } from "@base-ui/react/button";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { Spinner } from "./Spinner";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const BASE =
  "relative inline-flex shrink-0 cursor-pointer items-center justify-center whitespace-nowrap rounded-control border font-medium select-none " +
  "transition-[background-color,border-color,color,box-shadow] duration-(--duration-fast) ease-out " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
  "data-disabled:cursor-not-allowed data-disabled:not-aria-busy:opacity-50 aria-busy:cursor-progress";

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    "border-accent bg-accent text-on-accent shadow-[inset_0_1px_0_rgb(255_255_255/0.16)] " +
    "hover:not-data-disabled:border-accent-hover hover:not-data-disabled:bg-accent-hover",
  secondary:
    "border-border bg-surface text-fg shadow-raised " +
    "hover:not-data-disabled:border-border-strong/60 hover:not-data-disabled:bg-surface-hover active:not-data-disabled:bg-surface-active",
  ghost:
    "border-transparent bg-transparent text-fg-muted " +
    "hover:not-data-disabled:bg-surface-hover hover:not-data-disabled:text-fg active:not-data-disabled:bg-surface-active",
  danger:
    "border-fail-strong bg-fail-strong text-on-accent shadow-[inset_0_1px_0_rgb(255_255_255/0.14)] " +
    "hover:not-data-disabled:bg-[color-mix(in_oklab,var(--fail-strong)_86%,black)]",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-7 gap-1.5 px-2.5 text-13 [&_svg]:size-3.5",
  md: "h-8 gap-2 px-3 text-13 [&_svg]:size-4",
  lg: "h-10 gap-2 px-4 text-14 [&_svg]:size-4",
};

/** The class string of a button, for elements that must look like one (links, triggers). */
export function buttonClassName(
  variant: ButtonVariant = "secondary",
  size: ButtonSize = "md",
  className?: string,
): string {
  return cx(BASE, VARIANTS[variant], SIZES[size], className);
}

export interface ButtonProps extends Omit<BaseButton.Props, "className" | "children"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner, keeps the label and focus, and blocks further presses. */
  loading?: boolean;
  /** Leading icon; replaced by the spinner while loading. */
  icon?: ReactNode;
  trailingIcon?: ReactNode;
  className?: string;
  children?: ReactNode;
}

/**
 * The one button. Label with a verb that says what happens ("Deploy", "Restart service"),
 * and keep that verb through the flow: the toast after "Deploy" says "Deployed".
 */
export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  disabled = false,
  icon,
  trailingIcon,
  className,
  children,
  type = "button",
  ...rest
}: ButtonProps) {
  return (
    <BaseButton
      {...rest}
      type={type}
      disabled={disabled || loading}
      focusableWhenDisabled={loading}
      aria-busy={loading || undefined}
      className={buttonClassName(variant, size, className)}
    >
      {loading ? <Spinner size={size === "sm" ? 14 : 16} /> : icon}
      {children}
      {trailingIcon}
    </BaseButton>
  );
}

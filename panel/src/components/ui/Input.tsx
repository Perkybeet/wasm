import { Input as BaseInput } from "@base-ui/react/input";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

/** The frame shared by every text control: border, focus ring, invalid and disabled states. */
export const CONTROL_FRAME =
  "rounded-control border border-border-strong bg-surface text-fg shadow-raised " +
  "transition-[border-color,box-shadow] duration-(--duration-fast) ease-out " +
  "hover:border-fg-faint " +
  "has-[:focus-visible]:outline-2 has-[:focus-visible]:-outline-offset-1 has-[:focus-visible]:outline-focus has-[:focus-visible]:border-focus " +
  "has-[[aria-invalid=true]]:border-fail has-[[data-invalid]]:border-fail " +
  "has-[:disabled]:cursor-not-allowed has-[:disabled]:bg-bg-sunken has-[:disabled]:opacity-60 has-[:disabled]:hover:border-border-strong";

export interface InputProps extends Omit<BaseInput.Props, "className" | "size" | "prefix"> {
  size?: "sm" | "md";
  /** System values (paths, ports, hostnames) are typed in mono. */
  mono?: boolean;
  /** Leading icon, decorative. */
  icon?: ReactNode;
  /** Fixed text before the value, such as `https://`. */
  prefix?: ReactNode;
  /** Fixed text or a control after the value, such as a unit or a copy button. */
  suffix?: ReactNode;
  className?: string;
}

/** A single-line text control. Wrap it in Field for a label, help and error. */
export function Input({ size = "md", mono = false, icon, prefix, suffix, className, ...rest }: InputProps) {
  return (
    <div
      className={cx(
        "flex min-w-0 items-center",
        CONTROL_FRAME,
        size === "sm" ? "h-7 text-13" : "h-8 text-13",
        className,
      )}
    >
      {icon !== undefined ? (
        <span aria-hidden="true" className="flex pl-2.5 text-fg-faint [&_svg]:size-4">
          {icon}
        </span>
      ) : null}
      {prefix !== undefined ? (
        <span className="mono flex h-full items-center border-r border-border pr-2 pl-2.5 text-fg-faint select-none">
          {prefix}
        </span>
      ) : null}
      <BaseInput
        {...rest}
        className={cx(
          "h-full w-full min-w-0 flex-1 bg-transparent px-2.5 outline-none placeholder:text-fg-faint disabled:cursor-not-allowed",
          icon !== undefined && "pl-2",
          mono && "mono",
        )}
      />
      {suffix !== undefined ? (
        <span className="flex h-full shrink-0 items-center gap-1 pr-1 pl-1 text-fg-faint">{suffix}</span>
      ) : null}
    </div>
  );
}

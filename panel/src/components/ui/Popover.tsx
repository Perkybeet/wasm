import { Popover as BasePopover } from "@base-ui/react/popover";
import type { ReactElement, ReactNode } from "react";

import { cx } from "../../lib/cx";
import { POPUP_MOTION } from "./Tooltip";

export interface PopoverProps {
  trigger: ReactElement<Record<string, unknown>>;
  title?: string;
  description?: ReactNode;
  children?: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "center" | "end";
  /** Open on hover as well as press: for info tips that touch users can still tap. */
  openOnHover?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
}

/** Non-modal detail anchored to a control: an explanation, a small form, a preview. */
export function Popover({
  trigger,
  title,
  description,
  children,
  side = "bottom",
  align = "center",
  openOnHover = false,
  open,
  onOpenChange,
  className,
}: PopoverProps) {
  return (
    <BasePopover.Root
      {...(open !== undefined ? { open } : {})}
      {...(onOpenChange ? { onOpenChange: (next: boolean) => onOpenChange(next) } : {})}
    >
      <BasePopover.Trigger render={trigger} openOnHover={openOnHover} />
      <BasePopover.Portal>
        <BasePopover.Positioner side={side} align={align} sideOffset={6} className="z-50">
          <BasePopover.Popup
            className={cx(
              "w-80 max-w-[calc(100vw-2rem)] rounded-card border border-border bg-surface-raised p-4 text-fg shadow-overlay outline-none",
              POPUP_MOTION,
              className,
            )}
          >
            {title !== undefined ? <BasePopover.Title className="text-14 font-semibold">{title}</BasePopover.Title> : null}
            {description !== undefined ? (
              <BasePopover.Description className={cx("text-13 text-fg-muted", title !== undefined && "mt-1")}>
                {description}
              </BasePopover.Description>
            ) : null}
            {children !== undefined ? (
              <div className={cx(title !== undefined || description !== undefined ? "mt-3" : "")}>{children}</div>
            ) : null}
          </BasePopover.Popup>
        </BasePopover.Positioner>
      </BasePopover.Portal>
    </BasePopover.Root>
  );
}

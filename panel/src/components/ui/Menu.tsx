import { Menu as BaseMenu } from "@base-ui/react/menu";
import type { ReactElement, ReactNode } from "react";

import { cx } from "../../lib/cx";
import { Kbd } from "./Kbd";
import { POPUP_MOTION } from "./Tooltip";

export interface MenuProps {
  /** The element that opens the menu, usually a Button or IconButton. */
  trigger: ReactElement<Record<string, unknown>>;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "center" | "end";
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

/** A list of actions on one subject, opened from a button. */
export function Menu({ trigger, children, side = "bottom", align = "start", open, onOpenChange }: MenuProps) {
  return (
    <BaseMenu.Root
      {...(open !== undefined ? { open } : {})}
      {...(onOpenChange ? { onOpenChange: (next: boolean) => onOpenChange(next) } : {})}
    >
      <BaseMenu.Trigger render={trigger} />
      <BaseMenu.Portal>
        <BaseMenu.Positioner side={side} align={align} sideOffset={4} className="z-50 outline-none">
          <BaseMenu.Popup
            className={cx(
              "min-w-48 rounded-card border border-border bg-surface-raised p-1 text-fg shadow-overlay outline-none",
              POPUP_MOTION,
            )}
          >
            {children}
          </BaseMenu.Popup>
        </BaseMenu.Positioner>
      </BaseMenu.Portal>
    </BaseMenu.Root>
  );
}

export interface MenuItemProps {
  children: ReactNode;
  onClick?: () => void;
  icon?: ReactNode;
  /** Keys of the equivalent shortcut. */
  shortcut?: readonly string[];
  /** For actions that destroy something: set in the fail colour. */
  destructive?: boolean;
  disabled?: boolean;
}

export function MenuItem({ children, onClick, icon, shortcut, destructive = false, disabled = false }: MenuItemProps) {
  return (
    <BaseMenu.Item
      {...(onClick ? { onClick } : {})}
      disabled={disabled}
      className={cx(
        "flex h-8 cursor-pointer items-center gap-2.5 rounded-control px-2 text-13 outline-none select-none",
        "data-disabled:cursor-not-allowed data-disabled:opacity-50",
        destructive
          ? "text-fail data-highlighted:bg-fail-soft"
          : "text-fg data-highlighted:bg-surface-hover",
        "[&_svg]:size-4 [&_svg]:shrink-0",
      )}
    >
      {icon !== undefined ? (
        <span aria-hidden="true" className={cx("flex", destructive ? "text-fail" : "text-fg-muted")}>
          {icon}
        </span>
      ) : null}
      <span className="flex-1 truncate">{children}</span>
      {shortcut && shortcut.length > 0 ? (
        <span className="ml-4 flex gap-0.5" aria-hidden="true">
          {shortcut.map((key) => (
            <Kbd key={key}>{key}</Kbd>
          ))}
        </span>
      ) : null}
    </BaseMenu.Item>
  );
}

export function MenuSeparator() {
  return <BaseMenu.Separator className="-mx-1 my-1 h-px bg-border" />;
}

export function MenuGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <BaseMenu.Group>
      <BaseMenu.GroupLabel className="px-2 pt-1.5 pb-1 text-12 font-medium text-fg-faint select-none">
        {label}
      </BaseMenu.GroupLabel>
      {children}
    </BaseMenu.Group>
  );
}

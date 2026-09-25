import { Drawer as BaseDrawer } from "@base-ui/react/drawer";
import { X } from "lucide-react";
import type { ReactElement, ReactNode } from "react";

import { cx } from "../../lib/cx";
import { BACKDROP } from "./Dialog";
import { IconButton } from "./IconButton";

export interface DrawerProps {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  trigger?: ReactElement<Record<string, unknown>>;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  size?: "md" | "lg";
}

/**
 * A panel from the right edge for detail that keeps the page in view behind it: a deployment's
 * log, a unit file, a certificate. Swipe right to dismiss on touch screens.
 */
export function Drawer({
  title,
  description,
  children,
  footer,
  trigger,
  open,
  defaultOpen,
  onOpenChange,
  size = "md",
}: DrawerProps) {
  return (
    <BaseDrawer.Root
      swipeDirection="right"
      {...(open !== undefined ? { open } : {})}
      {...(defaultOpen !== undefined ? { defaultOpen } : {})}
      {...(onOpenChange ? { onOpenChange: (next: boolean) => onOpenChange(next) } : {})}
    >
      {trigger !== undefined ? <BaseDrawer.Trigger render={trigger} /> : null}
      <BaseDrawer.Portal>
        <BaseDrawer.Backdrop className={BACKDROP} />
        <BaseDrawer.Viewport className="fixed inset-0 z-50 flex justify-end">
          <BaseDrawer.Popup
            className={cx(
              "flex h-dvh w-full flex-col border-l border-border bg-surface-raised text-fg shadow-overlay outline-none",
              "[transform:translateX(var(--drawer-swipe-movement-x))] transition-transform duration-(--duration-base) ease-out",
              "data-starting-style:[transform:translateX(100%)] data-ending-style:[transform:translateX(100%)] data-swiping:duration-0",
              size === "md" ? "sm:max-w-[480px]" : "sm:max-w-[720px]",
            )}
          >
            <header className="flex items-start gap-4 border-b border-border px-5 py-4">
              <div className="min-w-0 flex-1">
                <BaseDrawer.Title className="title text-16 text-fg">{title}</BaseDrawer.Title>
                {description !== undefined ? (
                  <BaseDrawer.Description className="mt-1 text-13 text-fg-muted">{description}</BaseDrawer.Description>
                ) : null}
              </div>
              <BaseDrawer.Close
                render={<IconButton label="Close" icon={<X />} size="sm" tooltip={false} className="-mr-2" />}
              />
            </header>
            <BaseDrawer.Content className="min-h-0 flex-1 overflow-y-auto px-5 py-4 scroll-thin">{children}</BaseDrawer.Content>
            {footer !== undefined ? (
              <footer className="flex items-center justify-end gap-2 border-t border-border bg-bg-sunken px-5 py-3">
                {footer}
              </footer>
            ) : null}
          </BaseDrawer.Popup>
        </BaseDrawer.Viewport>
      </BaseDrawer.Portal>
    </BaseDrawer.Root>
  );
}

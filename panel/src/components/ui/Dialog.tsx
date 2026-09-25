import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import { X } from "lucide-react";
import type { ComponentType, ReactElement, ReactNode, RefObject } from "react";

import { cx } from "../../lib/cx";
import { IconButton } from "./IconButton";

export type DialogSize = "sm" | "md" | "lg";

const WIDTHS: Record<DialogSize, string> = {
  sm: "sm:max-w-[400px]",
  md: "sm:max-w-[520px]",
  lg: "sm:max-w-[720px]",
};

export const BACKDROP =
  "fixed inset-0 z-40 bg-backdrop transition-opacity duration-(--duration-base) ease-out " +
  "data-starting-style:opacity-0 data-ending-style:opacity-0";

export const MODAL_POPUP =
  "relative flex max-h-[calc(100dvh-2rem)] w-full flex-col overflow-hidden rounded-card border border-border bg-surface-raised text-fg shadow-overlay outline-none " +
  "transition-[opacity,scale] duration-(--duration-base) ease-out " +
  "data-starting-style:scale-[0.98] data-starting-style:opacity-0 data-ending-style:scale-[0.98] data-ending-style:opacity-0";

export interface DialogProps {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Buttons, right-aligned; the primary action goes last. */
  footer?: ReactNode;
  /** The element that opens the dialog. Omit it and control `open` instead. */
  trigger?: ReactElement<Record<string, unknown>>;
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  size?: DialogSize;
  /** Element to focus on open; defaults to the first focusable element. */
  initialFocus?: RefObject<HTMLElement | null>;
}

/** Shared frame of Dialog and ConfirmDialog: header, scrolling body, footer bar. */
export function DialogFrame({
  title,
  description,
  children,
  footer,
  Title,
  Description,
  close,
}: {
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  Title: ComponentType<{ className?: string; children?: ReactNode }>;
  Description: ComponentType<{ className?: string; children?: ReactNode }>;
  close?: ReactNode;
}) {
  return (
    <>
      <header className="flex items-start gap-4 px-5 pt-5 pb-1">
        <div className="min-w-0 flex-1">
          <Title className="title text-16 text-fg">{title}</Title>
          {description !== undefined ? (
            <Description className="mt-1 text-14 text-pretty text-fg-muted">{description}</Description>
          ) : null}
        </div>
        {close}
      </header>
      {children !== undefined ? <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4 scroll-thin">{children}</div> : <div className="h-4" />}
      {footer !== undefined ? (
        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-border bg-bg-sunken px-5 py-3">
          {footer}
        </footer>
      ) : null}
    </>
  );
}

/** A modal task that must be finished or dismissed before returning to the page. */
export function Dialog({
  title,
  description,
  children,
  footer,
  trigger,
  open,
  defaultOpen,
  onOpenChange,
  size = "md",
  initialFocus,
}: DialogProps) {
  return (
    <BaseDialog.Root
      {...(open !== undefined ? { open } : {})}
      {...(defaultOpen !== undefined ? { defaultOpen } : {})}
      {...(onOpenChange ? { onOpenChange: (next: boolean) => onOpenChange(next) } : {})}
    >
      {trigger !== undefined ? <BaseDialog.Trigger render={trigger} /> : null}
      <BaseDialog.Portal>
        <BaseDialog.Backdrop className={BACKDROP} />
        <BaseDialog.Viewport className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:items-start sm:pt-[12vh]">
          <BaseDialog.Popup
            {...(initialFocus ? { initialFocus } : {})}
            className={cx(MODAL_POPUP, WIDTHS[size])}
          >
            <DialogFrame
              title={title}
              description={description}
              footer={footer}
              Title={BaseDialog.Title}
              Description={BaseDialog.Description}
              close={
                <BaseDialog.Close
                  render={<IconButton label="Close" icon={<X />} size="sm" tooltip={false} className="-mt-1 -mr-2" />}
                />
              }
            >
              {children}
            </DialogFrame>
          </BaseDialog.Popup>
        </BaseDialog.Viewport>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}

/** A button that closes the enclosing Dialog. Pass a Button as `render`. */
export const DialogClose = BaseDialog.Close;

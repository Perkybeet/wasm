import { Toast } from "@base-ui/react/toast";
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { buttonClassName } from "./Button";
import { SystemOutput } from "./SystemOutput";
import type { ToastData, ToastKind } from "./toast";
import { isUrgent, toastManager } from "./toast";

const ICONS: Record<ToastKind, ReactNode> = {
  success: <CircleCheck className="text-ok" />,
  error: <CircleAlert className="text-fail" />,
  warning: <TriangleAlert className="text-warn" />,
  info: <Info className="text-fg-muted" />,
};

function isKind(value: string | undefined): value is ToastKind {
  return value === "success" || value === "error" || value === "warning" || value === "info";
}

/**
 * The open toasts of one urgency. Each group renders inside its own live region, so a toast
 * is announced by being shown: once, in the words on screen, with nothing hidden.
 */
function ToastList({ urgent }: { urgent: boolean }) {
  const { toasts } = Toast.useToastManager<ToastData>();
  return toasts
    .filter((item) => isUrgent(item.type) === urgent)
    .map((item) => {
      const kind = isKind(item.type) ? item.type : "info";
      return (
        // aria-hidden is cleared on the root and on the close button. Base UI hides both while
        // the stack is collapsed (and the root of any "high" priority toast), yet both stay in
        // the tab order: a focusable element inside an aria-hidden subtree, which a keyboard
        // reaches and a screen reader cannot name.
        <Toast.Root key={item.id} toast={item} aria-hidden={undefined} className="toast" swipeDirection={["right", "down"]}>
          <Toast.Content
            className={cx(
              "toast-content flex items-start gap-3 overflow-hidden rounded-card border border-border bg-surface-raised p-3 pr-2 text-fg shadow-overlay",
            )}
          >
            <span aria-hidden="true" className="mt-0.5 flex [&_svg]:size-4">
              {ICONS[kind]}
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              {/* Not a heading: a toast is not part of the page outline. */}
              <Toast.Title render={<div />} className="text-13 font-semibold text-fg" />
              <Toast.Description className="text-13 text-fg-muted" />
              {item.data?.detail !== undefined ? (
                <SystemOutput label="What the system said" maxHeight="max-h-28" className="mt-1.5 rounded-control bg-bg-sunken px-2 py-1.5">
                  {item.data.detail}
                </SystemOutput>
              ) : null}
              {item.data?.output !== undefined ? (
                <SystemOutput
                  label="The command's own output"
                  maxHeight="max-h-28"
                  className="mt-1.5 rounded-control bg-bg-sunken px-2 py-1.5"
                >
                  {item.data.output}
                </SystemOutput>
              ) : null}
              {item.actionProps ? <Toast.Action className={buttonClassName("secondary", "sm", "mt-2 self-start")} /> : null}
            </div>
            <Toast.Close
              aria-label="Dismiss notification"
              aria-hidden={undefined}
              className="flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-control text-fg-faint hover:bg-surface-hover hover:text-fg focus-visible:outline-2 focus-visible:outline-focus"
            >
              <X aria-hidden="true" className="size-3.5" />
            </Toast.Close>
          </Toast.Content>
        </Toast.Root>
      );
    });
}

/** Renders the toast queue. Mount once, at the app root, around everything. */
export function ToastProvider({ children }: { children: ReactNode }) {
  return (
    <Toast.Provider toastManager={toastManager} limit={3}>
      {children}
      <Toast.Portal>
        {/* The viewport itself is not live: the two regions inside it are, one per urgency.
            They exist before any toast does, so a toast added to one is announced, once, as
            it appears; failures interrupt (assertive), the rest wait their turn (polite). A
            toast updated in place is read again with its new words; one dismissed says
            nothing. The toasts are absolutely positioned against the viewport, so the regions
            add nothing to the layout. */}
        <Toast.Viewport aria-live="off" className="fixed right-4 bottom-4 z-[60] w-[min(380px,calc(100vw-2rem))] outline-none">
          <div aria-live="polite" aria-atomic="false" aria-relevant="additions text" data-toast-region="polite">
            <ToastList urgent={false} />
          </div>
          <div role="alert" aria-live="assertive" aria-atomic="false" aria-relevant="additions text" data-toast-region="assertive">
            <ToastList urgent />
          </div>
        </Toast.Viewport>
      </Toast.Portal>
    </Toast.Provider>
  );
}

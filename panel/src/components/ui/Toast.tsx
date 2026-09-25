import { Toast } from "@base-ui/react/toast";
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { buttonClassName } from "./Button";
import type { ToastData, ToastKind } from "./toast";
import { toastManager } from "./toast";

const ICONS: Record<ToastKind, ReactNode> = {
  success: <CircleCheck className="text-ok" />,
  error: <CircleAlert className="text-fail" />,
  warning: <TriangleAlert className="text-warn" />,
  info: <Info className="text-fg-muted" />,
};

function isKind(value: string | undefined): value is ToastKind {
  return value === "success" || value === "error" || value === "warning" || value === "info";
}

function ToastList() {
  const { toasts } = Toast.useToastManager<ToastData>();
  return toasts.map((item) => {
    const kind = isKind(item.type) ? item.type : "info";
    return (
      <Toast.Root key={item.id} toast={item} className="toast" swipeDirection={["right", "down"]}>
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
              <pre className="mt-1.5 max-h-28 overflow-auto rounded-control bg-bg-sunken px-2 py-1.5 text-12 whitespace-pre-wrap text-fg scroll-thin">
                {item.data.detail}
              </pre>
            ) : null}
            {item.actionProps ? <Toast.Action className={buttonClassName("secondary", "sm", "mt-2 self-start")} /> : null}
          </div>
          <Toast.Close
            aria-label="Dismiss notification"
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
        <Toast.Viewport className="fixed right-4 bottom-4 z-[60] w-[min(380px,calc(100vw-2rem))] outline-none">
          <ToastList />
        </Toast.Viewport>
      </Toast.Portal>
    </Toast.Provider>
  );
}

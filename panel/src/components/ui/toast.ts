import { Toast } from "@base-ui/react/toast";

export type ToastKind = "success" | "error" | "warning" | "info";

export interface ToastData {
  /** The system's own output, shown verbatim in mono under the description. */
  detail?: string;
  /** A failing tool's own output (psql's, git's or nginx's), verbatim, apart from `detail`. */
  output?: string;
}

export interface ToastOptions {
  description?: string;
  detail?: string;
  /** A failing tool's own output, verbatim, when it differs from `detail`. Never paraphrase it. */
  output?: string;
  action?: { label: string; onClick: () => void };
  /** Milliseconds before dismissal; 0 keeps it until closed. Errors default to 0. */
  timeout?: number;
  /** Reusing an id updates that toast in place instead of stacking a new one. */
  id?: string;
}

/**
 * The single toast queue. It lives outside React so that the API client and SSE handlers can
 * report outcomes; <ToastProvider> renders it.
 */
export const toastManager = Toast.createToastManager();

/** Failures interrupt a screen reader (assertive); everything else waits its turn (polite). */
export function isUrgent(kind: string | undefined): boolean {
  return kind === "error";
}

function add(kind: ToastKind, title: string, options: ToastOptions = {}): string {
  const { description, detail, output, action, timeout, id } = options;
  const data: ToastData = {
    ...(detail !== undefined ? { detail } : {}),
    // Never the same text twice: a tool whose output is its whole error carries nothing extra.
    ...(output !== undefined && output !== detail ? { output } : {}),
  };
  return toastManager.add<ToastData>({
    title,
    type: kind,
    // Always "low" for Base UI. Its "high" priority announces through a hidden alert copy and
    // aria-hides the visible toast, whose buttons stay focusable (axe: aria-hidden-focus).
    // Urgency is carried by the kind instead, and <ToastProvider> announces it (isUrgent).
    priority: "low",
    timeout: timeout ?? (kind === "error" ? 0 : 5000),
    data,
    ...(description !== undefined ? { description } : {}),
    ...(id !== undefined ? { id } : {}),
    ...(action ? { actionProps: { children: action.label, onClick: action.onClick } } : {}),
  });
}

/**
 * Report the outcome of an action. The title reuses the action's verb in the past tense:
 * "Deploy" produces "Deployed example.com".
 */
export const toast = {
  success: (title: string, options?: ToastOptions): string => add("success", title, options),
  error: (title: string, options?: ToastOptions): string => add("error", title, options),
  warning: (title: string, options?: ToastOptions): string => add("warning", title, options),
  info: (title: string, options?: ToastOptions): string => add("info", title, options),
  dismiss: (id?: string): void => {
    toastManager.close(id);
  },
};

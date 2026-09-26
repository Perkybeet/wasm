import { useRouter } from "@tanstack/react-router";
import type { ErrorComponentProps } from "@tanstack/react-router";
import { RotateCw } from "lucide-react";
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

import { Button } from "../components/ui/Button";
import { SystemOutput } from "../components/ui/SystemOutput";
import { describeError } from "../lib/errors";

/**
 * What a page shows when it cannot be displayed: the system's own message verbatim, the fix
 * above it when there is one, and a way out. Used by the error boundary and by the router
 * for a failed load.
 */
export function PageError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { hint, detail } = describeError(error);
  return (
    <section aria-labelledby="page-error-title" className="flex max-w-[72ch] flex-col gap-3 py-8">
      <h1 id="page-error-title" tabIndex={-1} data-page-title="" className="title text-24 text-fg outline-none">
        This page could not be displayed
      </h1>
      <p className="text-14 text-fg-muted">
        {hint ?? "Reload to try again. If it fails the same way, the message below is what to report."}
      </p>
      <SystemOutput label="The error" maxHeight="max-h-96" className="rounded-control border border-border bg-bg-sunken px-3 py-2.5 text-13">
        {detail}
      </SystemOutput>
      <div className="flex gap-2 pt-1">
        <Button
          variant="secondary"
          icon={<RotateCw aria-hidden="true" />}
          onClick={onRetry ?? (() => {
            window.location.reload();
          })}
        >
          {onRetry ? "Try again" : "Reload page"}
        </Button>
      </div>
    </section>
  );
}

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Clears a caught error when it changes: the shell passes the path, so leaving the page does. */
  resetKey?: unknown;
}

interface ErrorBoundaryState {
  error: unknown;
}

/**
 * Keeps a render failure inside the page that failed: the sidebar, the topbar and the
 * palette stay usable, so the operator can go somewhere else.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error };
  }

  override componentDidUpdate(previous: ErrorBoundaryProps): void {
    if (previous.resetKey !== this.props.resetKey && this.state.error !== null) this.setState({ error: null });
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error("A page failed to render:", error, info.componentStack);
  }

  override render() {
    if (this.state.error !== null) {
      return (
        <PageError
          error={this.state.error}
          onRetry={() => {
            this.setState({ error: null });
          }}
        />
      );
    }
    return this.props.children;
  }
}

/** The router's error view for a route whose load or render failed. */
export function RouteError({ error, reset }: ErrorComponentProps) {
  const router = useRouter();
  return (
    <PageError
      error={error}
      onRetry={() => {
        reset();
        void router.invalidate();
      }}
    />
  );
}

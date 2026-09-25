import { createFileRoute } from "@tanstack/react-router";

import { PageError } from "../app/ErrorBoundary";
import { Shell } from "../app/Shell";
import { SessionGate, requireSession } from "../features/auth/SessionGate";

/** Everything behind sign-in: the shell around every page of the console. */
export const Route = createFileRoute("/_console")({
  beforeLoad: ({ context, location }) => requireSession(context.queryClient, location.href),
  component: ConsoleLayout,
  errorComponent: ({ error }) => (
    <main className="mx-auto min-h-dvh max-w-3xl px-6 py-16">
      <PageError error={error} />
    </main>
  ),
});

function ConsoleLayout() {
  return (
    <SessionGate>
      <Shell />
    </SessionGate>
  );
}

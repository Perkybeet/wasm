import { createFileRoute } from "@tanstack/react-router";

import { Logo } from "../components/ui";

export const Route = createFileRoute("/")({
  component: Home,
});

// Placeholder until the shell and the overview page land (Tasks 2.3 and 3.1).
function Home() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center gap-4 px-6">
      <Logo size="lg" />
      <h1 className="title text-24">WASM Console</h1>
      <p className="text-14 text-fg-muted">The console is being built. Pages arrive with the next tasks.</p>
      {import.meta.env.DEV ? (
        <a href="/__design" className="text-14 font-medium text-accent-fg underline underline-offset-2">
          Open the design system
        </a>
      ) : null}
    </main>
  );
}

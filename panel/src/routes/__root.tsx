import { Outlet, createRootRoute } from "@tanstack/react-router";

export const Route = createRootRoute({
  component: Outlet,
  notFoundComponent: NotFound,
});

function NotFound() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center gap-2 px-6">
      <h1 className="title text-24">Page not found</h1>
      <p className="text-14 text-fg-muted">
        Nothing lives at this address. Check the link, or go back to the{" "}
        <a href="/" className="font-medium text-accent-fg underline underline-offset-2">
          overview
        </a>
        .
      </p>
    </main>
  );
}

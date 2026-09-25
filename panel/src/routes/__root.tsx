import type { QueryClient } from "@tanstack/react-query";
import { Link, Outlet, createRootRouteWithContext } from "@tanstack/react-router";

import { useDocumentTitle } from "../app/documentTitle";

export interface RouterContext {
  queryClient: QueryClient;
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: Outlet,
  notFoundComponent: NotFound,
});

function NotFound() {
  useDocumentTitle("Page not found");
  return (
    <main className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center gap-2 px-6">
      <h1 className="title text-24">Page not found</h1>
      <p className="text-14 text-fg-muted">
        Nothing lives at this address. Check the link, or go back to the{" "}
        <Link to="/" className="font-medium text-accent-fg underline underline-offset-2">
          overview
        </Link>
        .
      </p>
    </main>
  );
}

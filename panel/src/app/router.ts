import type { QueryClient } from "@tanstack/react-query";
import { createRouter } from "@tanstack/react-router";
import type { RouterHistory } from "@tanstack/react-router";

import { routeTree } from "../routeTree.gen";
import { RouteError } from "./ErrorBoundary";

/** Builds the router. `history` is for tests; the browser's history is the default. */
export function buildRouter(queryClient: QueryClient, history?: RouterHistory) {
  return createRouter({
    routeTree,
    context: { queryClient },
    defaultPreload: "intent",
    // TanStack Query owns freshness; the router must not keep its own copy of loader results.
    defaultPreloadStaleTime: 0,
    scrollRestoration: true,
    defaultErrorComponent: RouteError,
    ...(history ? { history } : {}),
  });
}

export type AppRouter = ReturnType<typeof buildRouter>;

declare module "@tanstack/react-router" {
  interface Register {
    router: AppRouter;
  }
}

/**
 * Builds the router. In development the design gallery is added at /__design; the import
 * sits behind import.meta.env.DEV, so production builds contain neither the route nor the
 * gallery code.
 */
export async function createAppRouter(queryClient: QueryClient): Promise<AppRouter> {
  if (import.meta.env.DEV) {
    const { registerDevRoutes } = await import("../dev/routes");
    registerDevRoutes(routeTree);
  }
  return buildRouter(queryClient);
}

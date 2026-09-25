import { createRouter } from "@tanstack/react-router";

import { routeTree } from "../routeTree.gen";

function buildRouter() {
  return createRouter({
    routeTree,
    defaultPreload: "intent",
    scrollRestoration: true,
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
export async function createAppRouter(): Promise<AppRouter> {
  if (import.meta.env.DEV) {
    const { registerDevRoutes } = await import("../dev/routes");
    registerDevRoutes(routeTree);
  }
  return buildRouter();
}

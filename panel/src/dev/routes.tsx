import { createRoute, lazyRouteComponent } from "@tanstack/react-router";
import type { AnyRoute } from "@tanstack/react-router";

/** Adds development-only routes to the file-based tree. Never imported in production. */
export function registerDevRoutes(root: AnyRoute): void {
  const design = createRoute({
    getParentRoute: () => root,
    path: "/__design",
    component: lazyRouteComponent(() => import("./DesignGallery"), "DesignGallery"),
  });
  const existing = (root.children ?? []) as AnyRoute[];
  if (existing.some((route) => route.path === "/__design" || route.path === "__design")) return;
  root.addChildren([...existing, design]);
}

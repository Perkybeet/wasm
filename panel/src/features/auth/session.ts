/**
 * What happens to the console when the session ends underneath it, and where the operator
 * goes back to after signing in again.
 */

import type { QueryClient } from "@tanstack/react-query";

import { configureApi } from "../../api/client";
import type { AppRouter } from "../../app/router";
import { cancelElevation, elevate } from "./elevation";

/**
 * Where to go after signing in: `next` when it is a path of this console, the overview
 * otherwise. Anything that could leave the origin (`//evil.example`, `/\evil`, a scheme) is
 * refused, so a crafted sign-in link cannot bounce the operator to another site.
 */
export function safeNext(next: string | undefined | null): string {
  if (typeof next !== "string" || !next.startsWith("/")) return "/";
  if (next.startsWith("//") || next.startsWith("/\\")) return "/";
  if (next === "/login" || next.startsWith("/login?") || next.startsWith("/login/")) return "/";
  return next;
}

/**
 * Wires the API client to the console: a lost session clears every cached answer (they
 * belong to a session that no longer exists) and lands on the sign-in page with a notice and
 * the way back; an action that needs elevation opens "Confirm it's you".
 *
 * @returns A function that removes the wiring.
 */
export function installSessionHandling(router: AppRouter, queryClient: QueryClient): () => void {
  let redirecting = false;
  return configureApi({
    elevate,
    onSessionExpired: () => {
      cancelElevation();
      const location = router.latestLocation;
      if (redirecting || location.pathname === "/login") return;
      redirecting = true;
      void queryClient.cancelQueries();
      queryClient.clear();
      void router
        .navigate({ to: "/login", search: { next: location.href, reason: "expired" }, replace: true })
        .finally(() => {
          redirecting = false;
        });
    },
  });
}

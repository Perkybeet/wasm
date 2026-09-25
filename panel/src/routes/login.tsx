import { createFileRoute, redirect } from "@tanstack/react-router";

import { sessionQuery } from "../api/queries/auth";
import { LoginPage } from "../features/auth/LoginPage";
import { safeNext } from "../features/auth/session";

export interface LoginSearch {
  next?: string;
  reason?: "expired";
}

export const Route = createFileRoute("/login")({
  validateSearch: (search: Record<string, unknown>): LoginSearch => ({
    ...(typeof search["next"] === "string" ? { next: search["next"] } : {}),
    ...(search["reason"] === "expired" ? { reason: "expired" as const } : {}),
  }),
  beforeLoad: async ({ context, search }) => {
    // An unreachable server is shown on the page itself, with the form still usable.
    const session = await context.queryClient.query(sessionQuery()).catch(() => null);
    if (session?.authenticated) {
      // eslint-disable-next-line @typescript-eslint/only-throw-error -- the router's redirect protocol
      throw redirect({ href: safeNext(search.next), replace: true });
    }
  },
  component: LoginRoute,
});

function LoginRoute() {
  const { next, reason } = Route.useSearch();
  return <LoginPage next={safeNext(next)} expired={reason === "expired"} />;
}

import { useQuery } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { redirect } from "@tanstack/react-router";
import { useEffect } from "react";
import type { ReactNode } from "react";

import { expireSession } from "../../api/client";
import { sessionQuery } from "../../api/queries/auth";
import type { SessionInfo } from "../../api/queries/auth";

/**
 * The console's `beforeLoad`: no session, no shell. Sends the operator to sign in with the
 * address they asked for, which the sign-in page returns them to.
 */
export async function requireSession(queryClient: QueryClient, href: string): Promise<SessionInfo> {
  const session = await queryClient.query(sessionQuery());
  if (!session.authenticated) {
    // eslint-disable-next-line @typescript-eslint/only-throw-error -- the router's redirect protocol
    throw redirect({ to: "/login", search: { next: href } });
  }
  return session;
}

/**
 * Keeps watching once the shell is up: the session query refetches when the tab regains
 * focus, and if the answer is now "not signed in" the operator is sent to sign in with the
 * "session expired" notice instead of meeting a failed request first.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const { data } = useQuery(sessionQuery());
  const lost = data !== undefined && !data.authenticated;
  useEffect(() => {
    if (lost) expireSession();
  }, [lost]);
  return lost ? null : children;
}

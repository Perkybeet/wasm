import { queryOptions } from "@tanstack/react-query";

import { request, setCsrfNames } from "../client";
import type { BodyOf, ResponseOf } from "../client";

export type SessionInfo = ResponseOf<"/api/auth/session", "get">;
export type LoginBody = BodyOf<"/api/auth/login", "post">;
export type ElevateBody = BodyOf<"/api/auth/elevate", "post">;

export const authKeys = {
  all: ["auth"] as const,
  session: ["auth", "session"] as const,
  twoFactor: ["auth", "2fa"] as const,
  sessions: ["auth", "sessions"] as const,
  tokens: ["auth", "tokens"] as const,
};

/**
 * Who is using the console, answered even before sign-in (`authenticated: false`), so the
 * console decides between the sign-in screen and the shell from one request.
 */
export const sessionQuery = () =>
  queryOptions({
    queryKey: authKeys.session,
    // No abort signal, on purpose: the router awaits this in beforeLoad, and a query whose
    // signal was consumed is cancelled when its last observer unmounts mid-flight, which
    // would reject the navigation with a CancelledError instead of an answer.
    queryFn: async () => {
      const session = await request("get", "/api/auth/session");
      setCsrfNames(session.csrf_header, session.csrf_cookie);
      return session;
    },
    staleTime: 60_000,
    // Coming back to a tab re-checks the session, which is how an expiry while the tab sat
    // in the background is noticed before the operator clicks something.
    refetchOnWindowFocus: "always",
  });

export function login(body: LoginBody) {
  return request("post", "/api/auth/login", { body });
}

export function logout() {
  return request("post", "/api/auth/logout");
}

export function elevateSession(body: ElevateBody) {
  return request("post", "/api/auth/elevate", { body });
}

/** A single-use credential for one WebSocket handshake. */
export async function wsTicket(): Promise<string> {
  const { ticket } = await request("post", "/api/auth/ws-ticket");
  return ticket;
}

export const twoFactorQuery = () =>
  queryOptions({
    queryKey: authKeys.twoFactor,
    queryFn: ({ signal }) => request("get", "/api/auth/2fa", { signal }),
  });

export const apiTokensQuery = () =>
  queryOptions({
    queryKey: authKeys.tokens,
    queryFn: ({ signal }) => request("get", "/api/auth/tokens", { signal }),
  });

import { queryOptions } from "@tanstack/react-query";

import { request, setCsrfNames } from "../client";
import type { BodyOf, ResponseOf } from "../client";

export type SessionInfo = ResponseOf<"/api/auth/session", "get">;
export type LoginBody = BodyOf<"/api/auth/login", "post">;
export type ElevateBody = BodyOf<"/api/auth/elevate", "post">;

export type TwoFactorStatus = ResponseOf<"/api/auth/2fa", "get">;
export type TwoFactorEnrollment = ResponseOf<"/api/auth/2fa/enroll", "post">;
export type ActiveSessions = ResponseOf<"/api/auth/sessions", "get">;
export type ActiveSession = ActiveSessions["sessions"][number];
export type ApiTokens = ResponseOf<"/api/auth/tokens", "get">;
export type ApiToken = ApiTokens["tokens"][number];
export type CreateTokenBody = BodyOf<"/api/auth/tokens", "post">;
export type CreatedToken = ResponseOf<"/api/auth/tokens", "post">;

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

/** Every live session: address, birth, last activity and expiry, the caller's own marked. */
export const sessionsQuery = () =>
  queryOptions({
    queryKey: authKeys.sessions,
    queryFn: ({ signal }) => request("get", "/api/auth/sessions", { signal }),
  });

/** Begins enrolment: a pending secret and its otpauth URI. The only answer carrying the secret. */
export function enrollTwoFactor() {
  return request("post", "/api/auth/2fa/enroll");
}

/** Activates the second factor with a code from the app; answers the backup codes, once. */
export function confirmTwoFactor(code: string) {
  return request("post", "/api/auth/2fa/confirm", { body: { code } });
}

/**
 * Replaces the backup codes with a new set, answered once; the old ones stop working. Needs a
 * recent "Confirm it's you", which the client asks for when the server says so.
 */
export function regenerateBackupCodes() {
  return request("post", "/api/auth/2fa/backup-codes");
}

/** Turns the second factor off. Needs a code and a recent "Confirm it's you". */
export function disableTwoFactor(code: string) {
  return request("post", "/api/auth/2fa/disable", { body: { code } });
}

/** Signs one other session out, named by the prefix the list shows. */
export function revokeSession(sidPrefix: string) {
  return request("delete", "/api/auth/sessions/{sid_prefix}", { params: { sid_prefix: sidPrefix } });
}

/**
 * Signs out every session but the caller's, in one call. Answers 400 when the caller's own
 * credential is not a browser session (a Bearer or the master token), which has no "other
 * session" to leave signed in.
 */
export function revokeOtherSessions() {
  return request("post", "/api/auth/sessions/revoke-others");
}

/** Issues a named, scoped token, returned in clear exactly once. Needs "Confirm it's you". */
export function createApiToken(body: CreateTokenBody) {
  return request("post", "/api/auth/tokens", { body });
}

/** Revokes a token: requests presenting it stop authenticating at once. */
export function revokeApiToken(id: number) {
  return request("delete", "/api/auth/tokens/{token_id}", { params: { token_id: id } });
}

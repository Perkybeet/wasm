/**
 * API tokens as the console presents them: what each scope allows, the expiry choices, and a
 * token's state from its record.
 */

import type { ApiToken } from "../../api/queries/auth";

export type TokenScope = "read" | "deploy" | "admin";

export interface ScopeOption {
  value: TokenScope;
  label: string;
  /** What a token of this scope can do, in the operator's words. */
  description: string;
}

/**
 * The scopes, weakest first. Each includes everything the one before it can do; the policy
 * itself is the backend's (wasm.web.auth.required_scope), this only says it in words.
 */
export const SCOPES: readonly ScopeOption[] = [
  {
    value: "read",
    label: "Read",
    description: "Looks, never changes: applications, deployments, logs, metrics and the state of the machine.",
  },
  {
    value: "deploy",
    label: "Deploy",
    description: "Read, plus create and update applications and roll them back. The one for CI.",
  },
  {
    value: "admin",
    label: "Admin",
    description: "Everything the console can do, including deleting, editing configuration and managing tokens.",
  },
];

export interface ExpiryOption {
  value: string;
  label: string;
  /** Lifetime in hours; null never expires. */
  hours: number | null;
}

export const EXPIRY_OPTIONS: readonly ExpiryOption[] = [
  { value: "7", label: "7 days", hours: 7 * 24 },
  { value: "30", label: "30 days", hours: 30 * 24 },
  { value: "90", label: "90 days", hours: 90 * 24 },
  { value: "365", label: "1 year", hours: 365 * 24 },
  { value: "never", label: "No expiry", hours: null },
];

export const DEFAULT_EXPIRY = "90";

export type TokenState = "active" | "expired" | "revoked";

/** Whether a token still authenticates, from its record. Revocation outranks expiry. */
export function tokenState(token: Pick<ApiToken, "expires_at" | "revoked_at">, now: number = Date.now()): TokenState {
  if (token.revoked_at !== null && token.revoked_at !== undefined) return "revoked";
  if (token.expires_at !== null && token.expires_at !== undefined && token.expires_at * 1000 <= now) return "expired";
  return "active";
}

const STATE_ORDER: Record<TokenState, number> = { active: 0, expired: 1, revoked: 2 };

/** Live tokens first, newest first within each state. */
export function sortTokens(tokens: readonly ApiToken[], now: number = Date.now()): ApiToken[] {
  return [...tokens].sort(
    (a, b) => STATE_ORDER[tokenState(a, now)] - STATE_ORDER[tokenState(b, now)] || b.created_at - a.created_at,
  );
}

const DAY = new Intl.DateTimeFormat("en-US", { year: "numeric", month: "short", day: "numeric" });

/** "expires Dec 24, 2026", or "never expires", for a Unix timestamp or null. */
export function expiryPhrase(expiresAt: number | null): string {
  return expiresAt === null ? "never expires" : `expires ${DAY.format(new Date(expiresAt * 1000))}`;
}

/**
 * The sign-in protection the panel is configured with, read from the `web` block of the
 * configuration. These are the values in config.yaml (merged over the shipped defaults); the
 * panel reads them when it starts, so an edit applies after `wasm web restart`.
 */

import type { ConsoleConfig } from "../../api/queries/config";
import { formatDuration } from "../../lib/format";

export interface LockoutPolicy {
  maxFailedAttempts: number | null;
  lockoutSeconds: number | null;
  rateLimitEnabled: boolean;
  rateLimitRequests: number | null;
  rateLimitWindowSeconds: number | null;
  sessionHours: number | null;
  ipAllowlist: readonly string[];
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function readLockoutPolicy(config: ConsoleConfig["config"]): LockoutPolicy {
  const raw = config["web"];
  const web = typeof raw === "object" && raw !== null && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const allowlist = web["ip_whitelist"];
  return {
    maxFailedAttempts: number(web["max_failed_attempts"]),
    lockoutSeconds: number(web["lockout_duration"]),
    rateLimitEnabled: web["rate_limit_enabled"] !== false,
    rateLimitRequests: number(web["rate_limit_requests"]),
    rateLimitWindowSeconds: number(web["rate_limit_window"]),
    sessionHours: number(web["token_expiration_hours"]),
    ipAllowlist: Array.isArray(allowlist) ? allowlist.filter((entry): entry is string => typeof entry === "string") : [],
  };
}

/** A whole number of seconds as people say it: "15 minutes", "1 hour", "90 seconds". */
export function spokenDuration(seconds: number): string {
  const units: [number, string][] = [
    [86_400, "day"],
    [3_600, "hour"],
    [60, "minute"],
  ];
  for (const [size, unit] of units) {
    if (seconds >= size && seconds % size === 0) {
      const amount = seconds / size;
      return `${String(amount)} ${unit}${amount === 1 ? "" : "s"}`;
    }
  }
  return seconds < 60 ? `${String(seconds)} second${seconds === 1 ? "" : "s"}` : formatDuration(seconds);
}

/** A rate's window as people say it: "a minute", "an hour", "every 90 seconds". */
export function perWindow(seconds: number): string {
  if (seconds === 1) return "a second";
  if (seconds === 60) return "a minute";
  if (seconds === 3600) return "an hour";
  return `every ${spokenDuration(seconds)}`;
}

/** A base32 secret in groups of four, the way authenticator apps show a key to type by hand. */
export function groupSecret(secret: string): string {
  return (secret.replace(/\s+/g, "").match(/.{1,4}/g) ?? []).join(" ");
}

/** The backup codes as a file to keep: one per line, with what they are for. */
export function backupCodesFile(codes: readonly string[], hostname: string): string {
  return [
    `WASM backup codes for ${hostname}`,
    "Each code signs in once in place of an authenticator code.",
    "",
    ...codes,
    "",
  ].join("\n");
}

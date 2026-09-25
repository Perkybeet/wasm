/**
 * Reading a DNS check: the verdict, which address is which, and what to do about it. Pure.
 */

import type { DnsCheck } from "../../api/queries/domains";

export type DnsVerdictKind = "here" | "elsewhere" | "missing";

export function dnsVerdict(check: Pick<DnsCheck, "points_here" | "resolved_addresses">): DnsVerdictKind {
  if (check.points_here) return "here";
  return check.resolved_addresses.length === 0 ? "missing" : "elsewhere";
}

/** IPv4 private ranges (RFC 1918) and carrier-grade NAT: addresses no public record points at. */
export function isPrivateAddress(address: string): boolean {
  const parts = address.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part))) return address.toLowerCase().startsWith("fd");
  const [a = 0, b = 0] = parts;
  return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127);
}

export interface DnsRecordAdvice {
  type: "A" | "AAAA";
  value: string;
}

/** The records to create so a name points here: one A per IPv4 address, one AAAA per IPv6. */
export function recordsToCreate(expected: readonly string[]): DnsRecordAdvice[] {
  return expected
    .filter((address) => !isPrivateAddress(address))
    .map((address) => ({ type: address.includes(":") ? "AAAA" : "A", value: address }));
}

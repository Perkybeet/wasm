/**
 * The admin-only audit log (`GET /api/audit`): every privileged action, from a sign-in to a
 * refused deletion, kept separately from `api/queries/jobs.ts` (owned by another page) because
 * nothing but the Activity page reads it. A caller without the `admin` scope gets a 403 - see
 * `features/activity/ActivityPage.tsx` for how that is shown.
 */

import { infiniteQueryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { QueryOf, ResponseOf } from "../client";

export type AuditListResponse = ResponseOf<"/api/audit", "get">;
export type AuditEntry = AuditListResponse["items"][number];

/** Filters of the audit log, exactly as the endpoint takes them (keyset: `before`). */
export type AuditFilters = NonNullable<QueryOf<"/api/audit", "get">>;

export const auditKeys = {
  all: ["audit"] as const,
  pages: (filters: Omit<AuditFilters, "before">) => ["audit", "pages", filters] as const,
};

/**
 * The audit log a page at a time, newest first. `before` walks strictly backward in time from
 * the previous page's `next_before`, so entries appended while the operator reads (a sign-in,
 * a denied action) never shift or repeat a row. See `mergeActivity` in
 * `features/activity/data.ts` for how this is combined with the jobs list, which has no such
 * cursor.
 */
export const auditPagesQuery = (filters: Omit<AuditFilters, "before"> = {}) =>
  infiniteQueryOptions({
    queryKey: auditKeys.pages(filters),
    queryFn: ({ signal, pageParam }) =>
      request("get", "/api/audit", {
        query: pageParam === null ? filters : { ...filters, before: pageParam },
        signal,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before ?? null,
  });

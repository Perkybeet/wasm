/**
 * The backups table's filters, in the URL (`/backups?domain=shop.example.com&database=1`) so a
 * filtered view can be bookmarked and shared, the way `features/apps/filters.ts` does it.
 * `domain` is answered server-side (`GET /api/backups?domain=...`); `database` (only backups
 * that include a database dump) has no server-side equivalent and is applied on what came back.
 */

import type { BackupList } from "../../api/queries/backups";

export type BackupRow = BackupList["backups"][number];

export interface BackupsSearch {
  domain?: string;
  /** Only backups that include a database dump. */
  database?: true;
}

function text(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed.slice(0, 200);
}

export function validateBackupsSearch(search: Record<string, unknown>): BackupsSearch {
  const domain = text(search["domain"]);
  const database = search["database"] === "1" || search["database"] === true;
  return {
    ...(domain !== undefined ? { domain } : {}),
    ...(database ? { database: true as const } : {}),
  };
}

export function isFiltered(search: BackupsSearch): boolean {
  return search.domain !== undefined || search.database === true;
}

/** Backups matching the client-side part of the filters (the domain filter is server-side). */
export function filterBackups(backups: readonly BackupRow[], search: BackupsSearch): BackupRow[] {
  if (search.database !== true) return [...backups];
  return backups.filter((backup) => backup.has_database);
}

/** Every domain that has at least one backup, alphabetically, for the domain filter. */
export function backupDomains(backups: readonly BackupRow[]): string[] {
  return [...new Set(backups.map((backup) => backup.domain))].sort();
}

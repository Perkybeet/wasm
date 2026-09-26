import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type BackupList = ResponseOf<"/api/backups", "get">;
export type Backup = ResponseOf<"/api/backups/{backup_id}", "get">;
export type BackupSchedules = ResponseOf<"/api/backup-schedules", "get">;
export type BackupSchedule = BackupSchedules["schedules"][number];
export type BackupStorage = ResponseOf<"/api/backups/storage", "get">;

export const backupKeys = {
  all: ["backups"] as const,
  list: (domain: string | null) => ["backups", "list", { domain }] as const,
  detail: (id: string) => ["backup", id] as const,
  storage: ["backups", "storage"] as const,
  schedules: ["backups", "schedules"] as const,
};

export const backupsQuery = (domain: string | null = null) =>
  queryOptions({
    queryKey: backupKeys.list(domain),
    queryFn: ({ signal }) => request("get", "/api/backups", { query: domain === null ? {} : { domain }, signal }),
  });

export const backupStorageQuery = () =>
  queryOptions({
    queryKey: backupKeys.storage,
    queryFn: ({ signal }) => request("get", "/api/backups/storage", { signal }),
  });

export const backupSchedulesQuery = () =>
  queryOptions({
    queryKey: backupKeys.schedules,
    queryFn: ({ signal }) => request("get", "/api/backup-schedules", { signal }),
  });

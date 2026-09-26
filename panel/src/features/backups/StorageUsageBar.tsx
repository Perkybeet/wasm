import { useQuery } from "@tanstack/react-query";

import { backupStorageQuery } from "../../api/queries/backups";
import type { BackupStorage } from "../../api/queries/backups";
import { ErrorBlock } from "../../components/page/QueryState";
import { Meter } from "../../components/ui/Progress";
import { Skeleton } from "../../components/ui/Skeleton";
import { formatBytes, formatCount } from "../../lib/format";

/** The filesystem the backup directory is on, when the server could read it. */
export function backupFilesystem(storage: BackupStorage): { total: number; free: number; used: number } | null {
  const total = storage.filesystem_total ?? null;
  const free = storage.filesystem_free ?? null;
  if (total === null || free === null || total <= 0) return null;
  return { total, free, used: Math.max(0, total - free) };
}

/** What the backups add up to, where they are kept, and how many of them there are. */
export function backupSummary(storage: BackupStorage): string {
  const backups = `${formatCount(storage.backup_count)} ${storage.backup_count === 1 ? "backup" : "backups"}`;
  const apps = `${formatCount(storage.domains.length)} ${storage.domains.length === 1 ? "application" : "applications"}`;
  return `${storage.total_size_human} in ${backups} of ${apps}, kept at `;
}

/**
 * How full the filesystem holding the backups is - that directory's own, not the machine's
 * root disk, which is a different one whenever backups live on a volume of their own - and
 * how much of it the backups take. Not a quota (WASM sets none on backups): context for
 * whether it is worth pruning old ones.
 */
export function StorageUsageBar() {
  const storage = useQuery(backupStorageQuery());

  if (storage.isError && storage.data === undefined) {
    return <ErrorBlock compact error={storage.error} title="Could not read backup storage usage" onRetry={() => void storage.refetch()} />;
  }
  if (storage.data === undefined) {
    return (
      // The loaded card's lines, each at its own height: the meter's label row and track, then
      // the line that sums up the backups.
      <div aria-hidden="true" className="flex flex-col gap-3 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
        <div className="flex flex-col gap-1.5">
          {/* The meter's label row sets its text on a baseline, a pixel taller than a line box. */}
          <div className="flex h-[1.3125rem] items-center justify-between gap-3">
            <Skeleton className="w-40" />
            <Skeleton className="w-48" />
          </div>
          {/* Skeleton's own line height would win over a thinner one: the track is clipped instead. */}
          <div className="h-1.5 overflow-hidden rounded-pill">
            <Skeleton />
          </div>
        </div>
        <div className="flex h-4 items-center">
          <Skeleton className="w-96 max-w-full" />
        </div>
      </div>
    );
  }

  const data = storage.data;
  const filesystem = backupFilesystem(data);

  return (
    <div className="flex flex-col gap-3 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
      {filesystem !== null ? (
        <Meter
          label="Disk holding the backups"
          value={filesystem.used}
          max={filesystem.total}
          valueText={`${formatBytes(filesystem.used)} used, ${formatBytes(filesystem.free)} free of ${formatBytes(filesystem.total)}`}
        />
      ) : (
        <div className="flex h-5 items-baseline justify-between gap-3">
          <span className="text-13 text-fg-muted">Disk holding the backups</span>
          <span className="text-13 text-fg-faint">Its size and free space could not be read</span>
        </div>
      )}
      <p className="text-12 text-fg-faint">
        {backupSummary(data)}
        <span translate="no" className="mono text-fg-muted">
          {data.path}
        </span>
      </p>
    </div>
  );
}

import { useQuery } from "@tanstack/react-query";

import { backupStorageQuery } from "../../api/queries/backups";
import { machineQuery } from "../../api/queries/system";
import { ErrorBlock } from "../../components/page/QueryState";
import { Meter } from "../../components/ui/Progress";
import { Skeleton } from "../../components/ui/Skeleton";
import { formatBytes, formatCount } from "../../lib/format";

/**
 * How much disk the backups directory holds, against the machine's total disk - not a quota
 * (WASM sets none on backups), context for whether it is worth pruning old ones.
 */
export function StorageUsageBar() {
  const storage = useQuery(backupStorageQuery());
  const machine = useQuery(machineQuery());

  if (storage.isError && storage.data === undefined) {
    return <ErrorBlock compact error={storage.error} title="Could not read backup storage usage" onRetry={() => void storage.refetch()} />;
  }
  if (storage.data === undefined) {
    return (
      <div className="flex flex-col gap-2 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
        <Skeleton className="h-3 w-40" />
        <Skeleton className="h-1.5 w-full rounded-pill" />
      </div>
    );
  }

  const data = storage.data;
  const diskTotal = machine.data?.disk.total ?? null;

  return (
    <div className="flex flex-col gap-3 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
      {diskTotal !== null && diskTotal > 0 ? (
        <Meter
          label="Backups"
          value={data.total_size}
          max={diskTotal}
          valueText={`${data.total_size_human} of ${formatBytes(diskTotal)} disk`}
        />
      ) : (
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-13 text-fg-muted">Backups</span>
          <span className="mono text-13 text-fg">{data.total_size_human}</span>
        </div>
      )}
      <p className="text-12 text-fg-faint">
        {`${formatCount(data.backup_count)} ${data.backup_count === 1 ? "backup" : "backups"}, ${formatCount(data.domains.length)} ${data.domains.length === 1 ? "application" : "applications"}, kept at `}
        <span translate="no" className="mono text-fg-muted">
          {data.path}
        </span>
      </p>
    </div>
  );
}

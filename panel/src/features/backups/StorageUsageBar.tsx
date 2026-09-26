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
  // The machine's disk decides between a meter and a bare figure, which differ in height:
  // until it answers (the top bar usually has it already), the placeholder holds the meter's.
  if (storage.data === undefined || (machine.data === undefined && !machine.isError)) {
    return (
      // The loaded card's lines, each at its own height: the meter's label row and track, then
      // the line that counts the backups.
      <div aria-hidden="true" className="flex flex-col gap-3 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
        <div className="flex flex-col gap-1.5">
          <div className="flex h-5 items-center justify-between gap-3">
            <Skeleton className="h-3 w-16" />
            <Skeleton className="h-3 w-32" />
          </div>
          <Skeleton className="h-1.5 w-full rounded-pill" />
        </div>
        <div className="flex h-4 items-center">
          <Skeleton className="h-3 w-80 max-w-full" />
        </div>
      </div>
    );
  }

  const data = storage.data;
  const diskTotal = machine.data?.disk.total ?? null;

  return (
    <div className="flex flex-col gap-3 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
      {diskTotal !== null && diskTotal > 0 ? (
        <Meter
          label="Storage"
          value={data.total_size}
          max={diskTotal}
          valueText={`${data.total_size_human} of ${formatBytes(diskTotal)}`}
        />
      ) : (
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-13 text-fg-muted">Storage</span>
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

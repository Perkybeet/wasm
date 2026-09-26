import { useQueryClient } from "@tanstack/react-query";

import { backupKeys } from "../../api/queries/backups";
import { isJobFinished } from "../../api/queries/jobs";
import { useServerEvent } from "../../realtime/events";

/** The backup jobs the backend runs (its JobType values). */
export const BACKUP_JOB_TYPES: ReadonlySet<string> = new Set(["backup", "restore"]);

/**
 * Refreshes the backups list and the storage figure whenever a backup or restore job ends,
 * whoever queued it: `create` and `restore` in `useBackupActions` only queue the job and toast
 * that it started, so without this the table and the storage bar stay exactly as they were
 * until something else happens to refetch them - the same fix `useCertificateRefresh` makes
 * for certificate jobs, and for the same reason: `isJobFinished` is the one place "has this
 * job ended" is decided, the same test `useFollowedJob`'s own polling relies on.
 */
export function useBackupRefresh(): void {
  const queryClient = useQueryClient();
  useServerEvent("job", (job) => {
    if (!BACKUP_JOB_TYPES.has(job.type) || !isJobFinished(job)) return;
    void queryClient.invalidateQueries({ queryKey: backupKeys.all });
    void queryClient.invalidateQueries({ queryKey: backupKeys.storage });
  });
}

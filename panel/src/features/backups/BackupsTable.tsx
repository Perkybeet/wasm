import { Link } from "@tanstack/react-router";
import { MoreHorizontal, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";

import type { Backup, BackupList } from "../../api/queries/backups";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Badge } from "../../components/ui/Badge";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { StatusPill } from "../../components/ui/StatusPill";
import { toast } from "../../components/ui/toast";
import { describeError } from "../../lib/errors";
import { RestoreBackupDialog } from "./RestoreBackupDialog";
import { useBackupActions } from "./useBackupActions";

export type BackupRow = BackupList["backups"][number];

interface Includes {
  label: string;
  present: boolean;
}

function includesOf(backup: BackupRow): Includes[] {
  return [
    { label: "env", present: backup.includes_env },
    { label: "database", present: backup.has_database },
    { label: "modules", present: backup.includes_node_modules },
    { label: "build", present: backup.includes_build },
  ];
}

/**
 * What the backup itself last recorded (`last_verified_at`, `verified_ok`), plus the moment
 * this session is waiting on a fresh check. The server, not the session, is the source of
 * truth: a page reload shows the same verdict, not "not checked" again.
 */
function VerifiedCell({ backup, checking }: { backup: BackupRow; checking: boolean }) {
  if (checking) return <StatusPill state="deploying" label="Checking" appearance="inline" size="sm" />;
  if (backup.verified_ok === true) {
    return (
      <span className="flex flex-col gap-0.5">
        <StatusPill state="running" label="Verified" appearance="inline" size="sm" />
        <RelativeTime value={backup.last_verified_at} className="text-12 text-fg-faint" />
      </span>
    );
  }
  if (backup.verified_ok === false) {
    return (
      <span className="flex flex-col gap-0.5">
        <StatusPill state="failed" label="Verification failed" appearance="inline" size="sm" />
        <RelativeTime value={backup.last_verified_at} className="text-12 text-fg-faint" />
      </span>
    );
  }
  return <StatusPill state="stopped" label="Never verified" appearance="inline" size="sm" />;
}

function RowActions({
  backup,
  checking,
  onVerify,
}: {
  backup: BackupRow;
  checking: boolean;
  onVerify: () => void;
}) {
  const { remove } = useBackupActions();
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <>
      <Menu align="end" trigger={<IconButton label={`Actions for ${backup.backup_id}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
        <MenuItem icon={<ShieldCheck />} disabled={checking} onClick={onVerify}>
          Verify
        </MenuItem>
        <MenuItem icon={<RotateCcw />} onClick={() => setRestoreOpen(true)}>
          Restore
        </MenuItem>
        <MenuItem icon={<Trash2 />} destructive onClick={() => setConfirmOpen(true)}>
          Delete
        </MenuItem>
      </Menu>
      <RestoreBackupDialog backup={backup} open={restoreOpen} onOpenChange={setRestoreOpen} />
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Delete ${backup.backup_id}`}
        description={`Permanently removes this backup of ${backup.domain} and its database dumps, if any. This cannot be undone.`}
        confirmText={backup.backup_id}
        actionLabel="Delete backup"
        onConfirm={async () => {
          await remove.mutateAsync(backup.backup_id);
        }}
      />
    </>
  );
}

export interface BackupsTableProps {
  backups: readonly BackupRow[];
  caption: string;
  loading?: boolean;
  /** Rows to hold while loading: the number the storage summary already counted, when known. */
  skeletonRows?: number;
  empty?: ReactNode;
}

/**
 * Every backup, newest first: what app, when, its size, what it includes, and its last
 * verification against its checksum - `last_verified_at` and `verified_ok`, as the backup
 * itself records them, so the state survives a reload instead of resetting to "not checked".
 */
export function BackupsTable({ backups, caption, loading = false, skeletonRows, empty }: BackupsTableProps) {
  const { verify } = useBackupActions();
  const [checking, setChecking] = useState<ReadonlySet<string>>(new Set());

  const onVerify = (backup: Backup): void => {
    setChecking((current) => new Set(current).add(backup.backup_id));
    verify.mutate(backup.backup_id, {
      onSuccess: (result) => {
        setChecking((current) => {
          const next = new Set(current);
          next.delete(backup.backup_id);
          return next;
        });
        if (result.valid) {
          toast.success(`${backup.backup_id} verified`);
        } else {
          toast.error(`${backup.backup_id} failed verification`, {
            detail: [...(result.errors ?? []), ...(result.warnings ?? [])].join("\n"),
          });
        }
      },
      onError: (error) => {
        setChecking((current) => {
          const next = new Set(current);
          next.delete(backup.backup_id);
          return next;
        });
        toast.error(`Could not verify ${backup.backup_id}`, { detail: describeError(error).detail });
      },
    });
  };

  const columns: Column<BackupRow>[] = [
    {
      id: "domain",
      header: "Application",
      cell: (row) => (
        <Link
          to="/apps/$domain"
          params={{ domain: row.domain }}
          className="-mx-1 rounded-[4px] px-1 py-0.5 font-medium text-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
        >
          {row.domain}
        </Link>
      ),
      sortValue: (row) => row.domain,
    },
    {
      id: "created",
      header: "Created",
      width: "w-36",
      cell: (row) => <RelativeTime value={row.timestamp} />,
      sortValue: (row) => row.timestamp,
    },
    {
      id: "size",
      header: "Size",
      align: "end",
      mono: true,
      width: "w-24",
      // On a phone the row keeps what, when and whether it was verified.
      hideBelow: "sm",
      cell: (row) => row.size_human,
      sortValue: (row) => row.size,
    },
    {
      id: "includes",
      header: "Includes",
      hideBelow: "md",
      cell: (row) => (
        <span className="flex flex-wrap gap-1">
          {includesOf(row)
            .filter((item) => item.present)
            .map((item) => (
              <Badge key={item.label} mono>
                {item.label}
              </Badge>
            ))}
        </span>
      ),
    },
    {
      id: "verified",
      header: "Verified",
      width: "w-36",
      cell: (row) => <VerifiedCell backup={row} checking={checking.has(row.backup_id)} />,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={backups}
      getRowId={(row) => row.backup_id}
      caption={caption}
      loading={loading}
      {...(skeletonRows !== undefined ? { skeletonRows } : {})}
      {...(empty !== undefined ? { empty } : {})}
      rowActions={(row) => (
        <RowActions backup={row} checking={checking.has(row.backup_id)} onVerify={() => onVerify(row)} />
      )}
      defaultSort={{ column: "created", direction: "descending" }}
    />
  );
}

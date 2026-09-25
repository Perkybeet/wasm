import { Link } from "@tanstack/react-router";
import { CircleCheck, CircleX, MoreHorizontal, RotateCcw, ShieldCheck, Trash2 } from "lucide-react";
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

type VerifyState = "unknown" | "checking" | "valid" | "invalid";

function VerifiedCell({ state }: { state: VerifyState }) {
  if (state === "checking") return <span className="text-fg-faint">Checking...</span>;
  if (state === "valid")
    return (
      <span className="inline-flex items-center gap-1 text-ok">
        <CircleCheck aria-hidden="true" className="size-3.5" />
        Verified
      </span>
    );
  if (state === "invalid")
    return (
      <span className="inline-flex items-center gap-1 text-fail">
        <CircleX aria-hidden="true" className="size-3.5" />
        Failed
      </span>
    );
  return <span className="text-fg-faint">Not checked</span>;
}

function RowActions({
  backup,
  verifyState,
  onVerify,
}: {
  backup: BackupRow;
  verifyState: VerifyState;
  onVerify: () => void;
}) {
  const { remove } = useBackupActions();
  const [restoreOpen, setRestoreOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <>
      <Menu align="end" trigger={<IconButton label={`Actions for ${backup.backup_id}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
        <MenuItem icon={<ShieldCheck />} disabled={verifyState === "checking"} onClick={onVerify}>
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
  empty?: ReactNode;
}

/**
 * Every backup, newest first: what app, when, its size, what it includes, and whether it was
 * verified against its checksum this session (the backend keeps no verified flag to list - see
 * `useBackupActions.verify` - so the state resets on reload rather than claiming to remember).
 */
export function BackupsTable({ backups, caption, loading = false, empty }: BackupsTableProps) {
  const { verify } = useBackupActions();
  const [verifications, setVerifications] = useState<ReadonlyMap<string, VerifyState>>(new Map());

  const onVerify = (backup: Backup): void => {
    setVerifications((current) => new Map(current).set(backup.backup_id, "checking"));
    verify.mutate(backup.backup_id, {
      onSuccess: (result) => {
        setVerifications((current) => new Map(current).set(backup.backup_id, result.valid ? "valid" : "invalid"));
        if (result.valid) {
          toast.success(`${backup.backup_id} verified`);
        } else {
          toast.error(`${backup.backup_id} failed verification`, {
            detail: [...(result.errors ?? []), ...(result.warnings ?? [])].join("\n"),
          });
        }
      },
      onError: (error) => {
        setVerifications((current) => new Map(current).set(backup.backup_id, "unknown"));
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
      cell: (row) => row.size_human,
      sortValue: (row) => row.size,
    },
    {
      id: "includes",
      header: "Includes",
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
      width: "w-28",
      cell: (row) => <VerifiedCell state={verifications.get(row.backup_id) ?? "unknown"} />,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={backups}
      getRowId={(row) => row.backup_id}
      caption={caption}
      loading={loading}
      {...(empty !== undefined ? { empty } : {})}
      rowActions={(row) => (
        <RowActions backup={row} verifyState={verifications.get(row.backup_id) ?? "unknown"} onVerify={() => onVerify(row)} />
      )}
      defaultSort={{ column: "created", direction: "descending" }}
    />
  );
}

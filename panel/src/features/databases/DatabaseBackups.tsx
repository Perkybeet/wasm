import { AlertDialog } from "@base-ui/react/alert-dialog";
import { useQuery } from "@tanstack/react-query";
import { Archive, MoreHorizontal, RotateCcw } from "lucide-react";
import { useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import type { DatabaseBackup } from "../../api/queries/databases";
import { databaseBackupsQuery } from "../../api/queries/databases";
import { ErrorBlock, QueryState } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { BACKDROP, DialogFrame, MODAL_POPUP, MODAL_VIEWPORT } from "../../components/ui/Dialog";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { Skeleton } from "../../components/ui/Skeleton";
import { cx } from "../../lib/cx";
import { useDatabaseActions } from "./useDatabaseActions";

function basename(path: string): string {
  return path.split("/").at(-1) ?? path;
}

function RestoreDialog({
  engine,
  database,
  backup,
  open,
  onOpenChange,
}: {
  engine: string;
  database: string;
  backup: DatabaseBackup;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const name = basename(backup.path);
  const inputRef = useRef<HTMLInputElement>(null);
  const [typed, setTyped] = useState("");
  const [dropExisting, setDropExisting] = useState(false);
  const { restoreBackup } = useDatabaseActions();
  const matches = typed === name;

  const close = (next: boolean): void => {
    if (!next && restoreBackup.isPending) return;
    onOpenChange(next);
    if (!next) {
      setTyped("");
      setDropExisting(false);
      restoreBackup.reset();
    }
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!matches) return;
    restoreBackup.mutate(
      { engine, database, backupName: name, dropExisting },
      { onSuccess: () => close(false) },
    );
  };

  return (
    <AlertDialog.Root open={open} onOpenChange={(next: boolean) => close(next)}>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className={BACKDROP} />
        <AlertDialog.Viewport className={MODAL_VIEWPORT}>
          <AlertDialog.Popup initialFocus={inputRef} className={cx(MODAL_POPUP, "sm:max-w-[460px]")}>
            <form onSubmit={submit} className="contents">
              <DialogFrame
                title={`Restore ${database}`}
                description={`Overwrites '${database}' with the contents of this dump. Anything written since ${name} was taken is lost.`}
                Title={AlertDialog.Title}
                Description={AlertDialog.Description}
                footer={
                  <>
                    <AlertDialog.Close render={<Button disabled={restoreBackup.isPending}>Cancel</Button>} />
                    <Button type="submit" variant="danger" disabled={!matches} loading={restoreBackup.isPending}>
                      Restore database
                    </Button>
                  </>
                }
              >
                <div className="flex flex-col gap-4">
                  <Checkbox
                    checked={dropExisting}
                    onCheckedChange={setDropExisting}
                    label="Drop the database first"
                    description="Recommended when the dump's schema differs from what is there now."
                  />
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor={`${name}-confirm`} className="text-13 text-fg-muted">
                      Type <span translate="no" className="mono rounded-[4px] bg-bg-sunken px-1 py-0.5 text-fg select-all">{name}</span> to confirm
                    </label>
                    <Input
                      id={`${name}-confirm`}
                      ref={inputRef}
                      mono
                      value={typed}
                      onValueChange={setTyped}
                      autoComplete="off"
                      autoCapitalize="off"
                      spellCheck={false}
                      disabled={restoreBackup.isPending}
                    />
                  </div>
                  {restoreBackup.isError ? (
                    <ErrorBlock live compact error={restoreBackup.error} title="The restore did not start" />
                  ) : null}
                </div>
              </DialogFrame>
            </form>
          </AlertDialog.Popup>
        </AlertDialog.Viewport>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}

function BackupActions({ engine, database, backup }: { engine: string; database: string; backup: DatabaseBackup }) {
  const [restoreOpen, setRestoreOpen] = useState(false);
  return (
    <>
      <Menu align="end" trigger={<IconButton label={`Actions for ${basename(backup.path)}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
        <MenuItem icon={<RotateCcw />} onClick={() => setRestoreOpen(true)}>
          Restore
        </MenuItem>
      </Menu>
      <RestoreDialog engine={engine} database={database} backup={backup} open={restoreOpen} onOpenChange={setRestoreOpen} />
    </>
  );
}

/** Dumps of this one database: create one, restore one by name with a typed confirmation. */
export function DatabaseBackups({ engine, database }: { engine: string; database: string }) {
  const backups = useQuery(databaseBackupsQuery(engine, database));
  const { createBackup } = useDatabaseActions();

  const columns: Column<DatabaseBackup>[] = [
    {
      id: "created",
      header: "Created",
      cell: (row) => <RelativeTime value={row.created} />,
      sortValue: (row) => row.created,
    },
    { id: "size", header: "Size", align: "end", mono: true, cell: (row) => row.size_human, sortValue: (row) => row.size },
    {
      id: "compressed",
      header: "Compressed",
      width: "w-28",
      // A fact, not a state: no colour.
      cell: (row) => <span className="text-fg-muted">{row.compressed ? "Yes" : "No"}</span>,
    },
  ];

  return (
    <Section
      title="Backups"
      description="Dumps of this database only, kept beside every other engine's dumps."
      // The empty state offers the same action; said once.
      actions={
        backups.data !== undefined && backups.data.backups.length > 0 ? (
          <Button
            size="sm"
            icon={<Archive aria-hidden="true" />}
            loading={createBackup.isPending}
            onClick={() => createBackup.mutate({ engine, database, compress: true })}
          >
            Create backup
          </Button>
        ) : undefined
      }
    >
      <QueryState
        query={backups}
        label="backups"
        skeleton={
          <div aria-hidden="true" className="flex flex-col gap-2">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-10 rounded-card" />
            ))}
          </div>
        }
        isEmpty={(data) => data.backups.length === 0}
        empty={
          <EmptyState
            icon={<Archive />}
            title="No backups of this database yet"
            description="A backup is a dump of this database alone, restorable by name."
            action={
              <Button
                variant="primary"
                icon={<Archive aria-hidden="true" />}
                loading={createBackup.isPending}
                onClick={() => createBackup.mutate({ engine, database, compress: true })}
              >
                Create backup
              </Button>
            }
          />
        }
      >
        {(data) => (
          <DataTable
            columns={columns}
            rows={data.backups}
            getRowId={(row) => row.path}
            caption={`Backups of ${database}`}
            rowActions={(row) => <BackupActions engine={engine} database={database} backup={row} />}
            defaultSort={{ column: "created", direction: "descending" }}
          />
        )}
      </QueryState>
    </Section>
  );
}

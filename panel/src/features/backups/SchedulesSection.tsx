import { useQuery } from "@tanstack/react-query";
import { CalendarClock, MoreHorizontal, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import type { BackupSchedule } from "../../api/queries/backups";
import { backupSchedulesQuery } from "../../api/queries/backups";
import { QueryState } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section } from "../../components/page/Section";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { Skeleton } from "../../components/ui/Skeleton";
import { ScheduleDialog } from "./ScheduleDialog";
import { useBackupActions } from "./useBackupActions";

function ScheduleActions({ schedule }: { schedule: BackupSchedule }) {
  const { deleteSchedule } = useBackupActions();
  const [editOpen, setEditOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  return (
    <>
      <Menu align="end" trigger={<IconButton label={`Actions for the schedule on ${schedule.domain}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
        <MenuItem icon={<CalendarClock />} onClick={() => setEditOpen(true)}>
          Edit
        </MenuItem>
        <MenuItem icon={<Trash2 />} destructive onClick={() => setConfirmOpen(true)}>
          Remove schedule
        </MenuItem>
      </Menu>
      <ScheduleDialog existing={schedule} open={editOpen} onOpenChange={setEditOpen} />
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Remove the schedule for ${schedule.domain}`}
        description="Stops automatic backups of this application. Backups already taken are kept."
        confirmText={schedule.domain}
        actionLabel="Remove schedule"
        destructive
        onConfirm={async () => {
          await deleteSchedule.mutateAsync(schedule.domain);
        }}
      />
    </>
  );
}

/** Automatic backups on a systemd timer, one per application, with the next run systemd reports. */
export function SchedulesSection() {
  const schedules = useQuery(backupSchedulesQuery());
  const [createOpen, setCreateOpen] = useState(false);

  const columns: Column<BackupSchedule>[] = [
    { id: "domain", header: "Application", cell: (row) => row.domain, sortValue: (row) => row.domain },
    {
      id: "schedule",
      header: "Schedule",
      cell: (row) => (
        <span className="flex flex-col">
          <span className="capitalize text-fg">{row.schedule}</span>
          <span translate="no" className="mono text-12 text-fg-faint">
            {row.on_calendar}
          </span>
        </span>
      ),
    },
    {
      id: "next_run",
      header: "Next run",
      cell: (row) => (row.next_run === "pending" ? <span className="text-fg-faint">Pending</span> : <RelativeTime value={row.next_run} />),
    },
    {
      id: "last_run",
      header: "Last run",
      hideBelow: "sm",
      cell: (row) => (row.last_run === "never" ? <span className="text-fg-faint">Never</span> : <RelativeTime value={row.last_run} />),
    },
    {
      id: "retention",
      header: "Retention",
      hideBelow: "md",
      cell: (row) =>
        row.retention_count !== null && row.retention_count !== undefined ? (
          <Badge mono>{`${String(row.retention_count)} backups`}</Badge>
        ) : (
          <span className="text-fg-faint">Unknown</span>
        ),
    },
  ];

  return (
    <Section
      title="Schedules"
      description="Automatic backups, one per application, on a systemd timer."
      actions={
        <Button size="sm" variant="primary" icon={<Plus aria-hidden="true" />} onClick={() => setCreateOpen(true)}>
          New schedule
        </Button>
      }
    >
      <QueryState
        query={schedules}
        label="backup schedules"
        skeleton={
          <div aria-hidden="true" className="flex flex-col gap-2">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-10 rounded-card" />
            ))}
          </div>
        }
        isEmpty={(data) => data.schedules.length === 0}
        empty={
          <EmptyState
            icon={<CalendarClock />}
            title="No scheduled backups"
            description="Set an application to back itself up on a schedule, without anyone starting it by hand."
            action={
              <Button variant="primary" icon={<Plus aria-hidden="true" />} onClick={() => setCreateOpen(true)}>
                New schedule
              </Button>
            }
            command="wasm backup schedule <domain> --schedule daily"
          />
        }
      >
        {(data) => (
          <DataTable
            columns={columns}
            rows={data.schedules}
            getRowId={(row) => row.domain}
            caption="Backup schedules"
            rowActions={(row) => <ScheduleActions schedule={row} />}
            defaultSort={{ column: "domain", direction: "ascending" }}
          />
        )}
      </QueryState>
      <ScheduleDialog open={createOpen} onOpenChange={setCreateOpen} />
    </Section>
  );
}

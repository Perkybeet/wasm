import { History, MoreHorizontal, Pencil, Play, ToggleLeft, ToggleRight, Trash2 } from "lucide-react";
import { useState } from "react";

import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem, MenuSeparator } from "../../components/ui/Menu";
import type { CronJob } from "./data";
import { useCronActions } from "./useCronActions";

export interface CronJobRowActionsProps {
  job: CronJob;
  onEdit: (job: CronJob) => void;
  onViewRuns: (name: string) => void;
}

/** The menu at the end of a cron job's row: run it now, edit, enable/disable, its history, delete. */
export function CronJobRowActions({ job, onEdit, onViewRuns }: CronJobRowActionsProps) {
  const { run, enable, disable, remove } = useCronActions();
  const [deleteOpen, setDeleteOpen] = useState(false);
  const name = job.name;

  return (
    <>
      <Menu align="end" trigger={<IconButton label={`Actions for ${name}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
        <MenuItem icon={<Play />} disabled={run.isPending} onClick={() => run.mutate(name)}>
          Run now
        </MenuItem>
        <MenuItem icon={<History />} onClick={() => onViewRuns(name)}>
          View runs
        </MenuItem>
        <MenuItem icon={<Pencil />} onClick={() => onEdit(job)}>
          Edit
        </MenuItem>
        <MenuSeparator />
        {job.enabled ? (
          <MenuItem icon={<ToggleLeft />} disabled={disable.isPending} onClick={() => disable.mutate(name)}>
            Disable
          </MenuItem>
        ) : (
          <MenuItem icon={<ToggleRight />} disabled={enable.isPending} onClick={() => enable.mutate(name)}>
            Enable
          </MenuItem>
        )}
        <MenuSeparator />
        <MenuItem icon={<Trash2 />} destructive onClick={() => setDeleteOpen(true)}>
          Delete job
        </MenuItem>
      </Menu>

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete ${name}`}
        description="Removes the timer and its service unit. This cannot be undone."
        confirmText={name}
        actionLabel="Delete job"
        onConfirm={async () => {
          await remove.mutateAsync(name);
        }}
      />
    </>
  );
}

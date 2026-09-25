import { useNavigate } from "@tanstack/react-router";
import { CircleArrowUp, History, MoreHorizontal, Play, RotateCw, Square, Trash2 } from "lucide-react";
import { useState } from "react";

import { ElevationCancelledError } from "../../api/errors";
import type { App } from "../../api/queries/apps";
import type { Job } from "../../api/queries/jobs";
import { appStatus } from "../../components/page/status";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { Dialog } from "../../components/ui/Dialog";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem, MenuSeparator } from "../../components/ui/Menu";
import { toast } from "../../components/ui/toast";
import { hasUnit } from "../apps/AppRowActions";
import { reportActionError, useAppActions } from "../apps/useAppActions";
import { RollbackDialog } from "./RollbackDialog";
import { useConfirmItsYou, useDeleteApp } from "./useDeleteApp";

export interface AppActionsProps {
  app: App;
  /** A job on this app is queued or running: Update waits for it. */
  busy: boolean;
  onJobQueued: (job: Job) => void;
}

/**
 * The app's actions in its header: Restart and Update (the primary action) as buttons, the
 * rest in a menu. On a phone everything folds into the one menu. Stopping asks first; Delete
 * asks for the domain to be typed; both go through "Confirm it's you" in the API client when
 * the session is not elevated.
 */
export function AppActions({ app, busy, onJobQueued }: AppActionsProps) {
  const domain = app.domain;
  const navigate = useNavigate();
  const { restart, start, stop, update } = useAppActions(domain, { onJobQueued });
  const remove = useDeleteApp(domain);
  const confirmItsYou = useConfirmItsYou();
  const [confirmStop, setConfirmStop] = useState(false);
  const [rollbackOpen, setRollbackOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const unit = hasUnit(app);
  const running = appStatus(app.status).state === "running";

  const unitItem = unit ? (
    running ? (
      <MenuItem icon={<Square />} disabled={stop.isPending} onClick={() => setConfirmStop(true)}>
        Stop
      </MenuItem>
    ) : (
      <MenuItem icon={<Play />} disabled={start.isPending} onClick={() => start.mutate()}>
        Start
      </MenuItem>
    )
  ) : null;

  const rest = (
    <>
      <MenuItem icon={<History />} onClick={() => setRollbackOpen(true)}>
        Roll back
      </MenuItem>
      <MenuSeparator />
      <MenuItem
        icon={<Trash2 />}
        destructive
        onClick={() => {
          // Sudo mode first, so "Confirm it's you" never opens on top of the typed confirmation.
          confirmItsYou().then(
            () => {
              setDeleteOpen(true);
            },
            (error: unknown) => {
              if (!(error instanceof ElevationCancelledError)) reportActionError(`Deletion of ${domain} could not start`, error);
            },
          );
        }}
      >
        Delete application
      </MenuItem>
    </>
  );

  return (
    <>
      <div className="hidden items-center gap-2 sm:flex">
        {unit ? (
          <Button icon={<RotateCw aria-hidden="true" />} loading={restart.isPending} onClick={() => restart.mutate()}>
            Restart
          </Button>
        ) : null}
        <Button
          variant="primary"
          icon={<CircleArrowUp aria-hidden="true" />}
          loading={update.isPending || busy}
          onClick={() => update.mutate()}
        >
          Update
        </Button>
        <Menu align="end" trigger={<IconButton variant="secondary" label="More actions" icon={<MoreHorizontal />} tooltip={false} />}>
          {unitItem}
          {rest}
        </Menu>
      </div>

      <div className="sm:hidden">
        <Menu
          align="end"
          trigger={<IconButton variant="secondary" label={`Actions for ${domain}`} icon={<MoreHorizontal />} tooltip={false} />}
        >
          <MenuItem icon={<CircleArrowUp />} disabled={update.isPending || busy} onClick={() => update.mutate()}>
            Update
          </MenuItem>
          {unit ? (
            <MenuItem icon={<RotateCw />} disabled={restart.isPending} onClick={() => restart.mutate()}>
              Restart
            </MenuItem>
          ) : null}
          {unitItem}
          {rest}
        </Menu>
      </div>

      <Dialog
        open={confirmStop}
        onOpenChange={setConfirmStop}
        size="sm"
        title={`Stop ${domain}?`}
        description="The service stops and the site answers 502 until it is started again. Nothing is deleted."
        footer={
          <>
            <Button onClick={() => setConfirmStop(false)}>Cancel</Button>
            <Button
              variant="danger"
              loading={stop.isPending}
              onClick={() =>
                stop.mutate(undefined, {
                  onSettled: () => {
                    setConfirmStop(false);
                  },
                })
              }
            >
              Stop application
            </Button>
          </>
        }
      />

      <RollbackDialog domain={domain} layout={app.layout} open={rollbackOpen} onOpenChange={setRollbackOpen} onJobQueued={onJobQueued} />

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete ${domain}`}
        description="Stops and removes the service, the site, the certificate and the app's files. Backups are kept. This cannot be undone."
        confirmText={domain}
        actionLabel="Delete application"
        onConfirm={async () => {
          await remove.mutateAsync({ removeFiles: true, removeSsl: true });
          // The page is about to go; the toast is what stays to say the job is on its way.
          toast.info(`Deletion of ${domain} queued`, { description: "You will be told when it finishes." });
          void navigate({ to: "/apps" });
        }}
      />
    </>
  );
}

import { useNavigate } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";
import { useState } from "react";

import { ElevationCancelledError } from "../../../api/errors";
import type { App } from "../../../api/queries/apps";
import { DangerZone } from "../../../components/page/DangerZone";
import { Button } from "../../../components/ui/Button";
import { Checkbox } from "../../../components/ui/Checkbox";
import { ConfirmDialog } from "../../../components/ui/ConfirmDialog";
import { toast } from "../../../components/ui/toast";
import { reportActionError } from "../../apps/useAppActions";
import { useConfirmItsYou, useDeleteApp } from "../useDeleteApp";

/** Exactly what a deletion with these options removes and keeps. */
export function deletionSummary(app: Pick<App, "path">, removeFiles: boolean, removeSsl: boolean): string {
  const gone = ["the service", "the web server site"];
  if (removeSsl) gone.push("the certificate");
  if (removeFiles) gone.push(app.path ? `the files in ${app.path}` : "the app's files");
  const list = `${gone.slice(0, -1).join(", ")} and ${gone.at(-1) ?? ""}`;
  const kept = ["backups"];
  if (!removeSsl) kept.push("the certificate");
  if (!removeFiles) kept.push("the files");
  const keptList = kept.length === 1 ? kept[0] ?? "" : `${kept.slice(0, -1).join(", ")} and ${kept.at(-1) ?? ""}`;
  return `Stops the app and removes ${list}. Kept: ${keptList}. This cannot be undone.`;
}

/**
 * Deleting the app: the choice of what goes with it first, then "Confirm it's you", then the
 * domain typed out. Queued as a job, like every change that takes a while.
 */
export function DangerSection({ app }: { app: App }) {
  const domain = app.domain;
  const navigate = useNavigate();
  const remove = useDeleteApp(domain);
  const confirmItsYou = useConfirmItsYou();
  const [removeFiles, setRemoveFiles] = useState(true);
  const [removeSsl, setRemoveSsl] = useState(true);
  const [open, setOpen] = useState(false);

  const start = (): void => {
    confirmItsYou().then(
      () => {
        setOpen(true);
      },
      (error: unknown) => {
        if (!(error instanceof ElevationCancelledError)) reportActionError(`Deletion of ${domain} could not start`, error);
      },
    );
  };

  return (
    <DangerZone>
      <div className="flex flex-col gap-4 px-5 py-4 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
        <div className="flex min-w-0 flex-col gap-3">
          <div>
            <h3 className="text-14 font-medium text-fg">Delete this application</h3>
            <p className="mt-0.5 max-w-[60ch] text-13 text-pretty text-fg-muted">
              Stops it and removes its service and its web server site. Backups are kept either way.
            </p>
          </div>
          <div className="flex flex-col gap-2.5">
            <Checkbox
              label="Also delete its files"
              description={app.path ? `Everything in ${app.path}, the .env included.` : "The app's directory, the .env included."}
              checked={removeFiles}
              onCheckedChange={setRemoveFiles}
            />
            <Checkbox
              label="Also delete its certificate"
              description="The certificate issued for this domain."
              checked={removeSsl}
              onCheckedChange={setRemoveSsl}
            />
          </div>
        </div>
        <div className="shrink-0">
          <Button variant="danger" icon={<Trash2 aria-hidden="true" />} onClick={start}>
            Delete application
          </Button>
        </div>
      </div>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Delete ${domain}`}
        description={deletionSummary(app, removeFiles, removeSsl)}
        confirmText={domain}
        actionLabel="Delete application"
        onConfirm={async () => {
          await remove.mutateAsync({ removeFiles, removeSsl });
          // The page is about to go; the toast is what stays to say the job is on its way.
          toast.info(`Deletion of ${domain} queued`, { description: "You will be told when it finishes." });
          void navigate({ to: "/apps" });
        }}
      />
    </DangerZone>
  );
}

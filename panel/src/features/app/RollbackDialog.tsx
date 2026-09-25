import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";

import type { Job } from "../../api/queries/jobs";
import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Skeleton } from "../../components/ui/Skeleton";
import { cx } from "../../lib/cx";
import { formatBytes } from "../../lib/format";
import { useAppActions } from "../apps/useAppActions";
import { releasesQuery, rollbackPointsQuery } from "./queries";

interface Target {
  id: string;
  title: string;
  commit: string | null;
  when: string | null;
  note: string | null;
}

function Options({
  name,
  targets,
  value,
  onChange,
  legend,
}: {
  name: string;
  targets: readonly Target[];
  value: string | null;
  onChange: (id: string) => void;
  legend: string;
}) {
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-2 text-13 text-fg-muted">{legend}</legend>
      {targets.map((target) => (
        <label
          key={target.id}
          className={cx(
            "grid cursor-pointer grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-0.5 rounded-control border px-3 py-2.5",
            "has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-1 has-[:focus-visible]:outline-focus",
            value === target.id ? "border-accent bg-accent-soft" : "border-border hover:bg-surface-hover",
          )}
        >
          <input
            type="radio"
            name={name}
            value={target.id}
            checked={value === target.id}
            onChange={() => onChange(target.id)}
            className="row-span-2 mt-0.5 size-4 shrink-0 accent-accent"
          />
          <span translate="no" className="mono truncate text-12 text-fg">
            {target.title}
          </span>
          <span className="col-start-2 flex flex-wrap items-center gap-x-2 text-12 text-fg-muted">
            {target.commit ? <span className="mono">{target.commit.slice(0, 7)}</span> : null}
            {target.when ? <RelativeTime value={target.when} /> : null}
            {target.note ? <span>{target.note}</span> : null}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

export interface RollbackDialogProps {
  domain: string;
  /** The app's deploy layout: `releases`, or `inplace` for an app not migrated yet. */
  layout: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onJobQueued: (job: Job) => void;
}

/**
 * Puts an earlier version of the app back. An app on releases switches to a previous release
 * in seconds; an app deployed in place is restored from one of its backups, as a job.
 */
export function RollbackDialog({ domain, layout, open, onOpenChange, onJobQueued }: RollbackDialogProps) {
  const name = useId();
  const [choice, setChoice] = useState<string | null>(null);
  const onReleases = layout === "releases";
  const releases = useQuery({ ...releasesQuery(domain), enabled: open && onReleases });
  // The API also answers "no releases" for an app it finds in place, whatever it was listed as.
  const inPlace = !onReleases || releases.data === null;
  const points = useQuery({ ...rollbackPointsQuery(domain), enabled: open && inPlace });
  const { activateRelease, rollbackToBackup } = useAppActions(domain, { onJobQueued });
  const action = inPlace ? rollbackToBackup : activateRelease;

  const targets: Target[] | undefined = inPlace
    ? points.data?.items.map((point) => ({
        id: point.id,
        title: point.id,
        commit: point.git_commit ?? null,
        when: point.created_at,
        note: `${point.description}, ${formatBytes(point.size_bytes)}`,
      }))
    : releases.data?.items
        .filter((release) => !release.active && release.on_disk)
        .map((release) => ({
          id: release.id,
          title: release.id,
          commit: release.commit ?? null,
          when: release.activated_at ?? release.created_at,
          note: release.status,
        }));

  const loading = inPlace ? points.isPending : releases.isPending;
  const loadError = !inPlace && releases.isError ? releases.error : inPlace && points.isError ? points.error : null;

  const close = (next: boolean): void => {
    if (!next && action.isPending) return;
    onOpenChange(next);
    if (!next) {
      setChoice(null);
      action.reset();
    }
  };

  const confirm = (): void => {
    if (choice === null) return;
    action.mutate(choice, {
      onSuccess: () => {
        close(false);
      },
    });
  };

  let body;
  if (loadError !== null) {
    body = <ErrorBlock compact error={loadError} title="Could not list what this app can roll back to" />;
  } else if (loading || targets === undefined) {
    body = (
      <div aria-busy="true" className="flex flex-col gap-2">
        <span className="sr-only">Loading rollback targets</span>
        {[0, 1].map((i) => (
          <Skeleton key={i} className="h-14 rounded-control" />
        ))}
      </div>
    );
  } else if (targets.length === 0) {
    body = (
      <p className="text-14 text-fg-muted">
        {inPlace
          ? "There is no backup of this app to roll back to. Backups are taken before every deploy and on the schedule set in Backups."
          : "There is no earlier release on disk. Every deploy keeps the previous releases, up to the retention limit."}
      </p>
    );
  } else {
    body = (
      <Options
        name={name}
        targets={targets}
        value={choice}
        onChange={setChoice}
        legend={inPlace ? "Restore the app's files from a backup:" : "Switch back to an earlier release:"}
      />
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={`Roll back ${domain}`}
      description={
        inPlace
          ? "This app is deployed in place, so a rollback restores a backup and restarts it. Anything written since the backup is replaced."
          : "The app switches to the chosen release and restarts; if it fails its health check it is switched back."
      }
      footer={
        <>
          <Button disabled={action.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button variant="primary" disabled={choice === null} loading={action.isPending} onClick={confirm}>
            Roll back
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {body}
        {action.isError ? <ErrorBlock live compact error={action.error} title="The rollback did not start" /> : null}
      </div>
    </Dialog>
  );
}

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArchiveRestore } from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";

import { releasesQuery, rollbackPointsQuery } from "../../../api/queries/apps";
import type { Release, RollbackPoint } from "../../../api/queries/apps";
import { deploymentKeys } from "../../../api/queries/deployments";
import { ErrorBlock } from "../../../components/page/QueryState";
import { RelativeTime } from "../../../components/page/RelativeTime";
import { Section } from "../../../components/page/Section";
import { Badge } from "../../../components/ui/Badge";
import { Button } from "../../../components/ui/Button";
import { Dialog } from "../../../components/ui/Dialog";
import { Skeleton } from "../../../components/ui/Skeleton";
import { formatBytes } from "../../../lib/format";
import { useAppActions } from "../../apps/useAppActions";
import { releaseBadge, shortCommit } from "./words";

const LINK =
  "rounded-[4px] font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus";

function Panel({ children }: { children: ReactNode }) {
  return <div className="min-w-0 rounded-card border border-border bg-surface shadow-raised">{children}</div>;
}

function ListSkeleton() {
  return (
    <Panel>
      <div aria-hidden="true" className="flex flex-col divide-y divide-border">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex flex-col gap-2 px-4 py-3">
            <Skeleton className="h-3 w-44" />
            <Skeleton className="h-3 w-28" />
          </div>
        ))}
      </div>
    </Panel>
  );
}

/** One release: its id, its commit, when it was built or began serving, and what can be done with it. */
function ReleaseRow({ release, active, onActivate }: { release: Release; active: Release | undefined; onActivate: (release: Release) => void }) {
  const badge = releaseBadge(release.active ? "active" : release.status);
  const commit = shortCommit(release.commit);
  const older = active !== undefined && release.id < active.id;
  let action: ReactNode = null;
  if (!release.active && release.on_disk) {
    action = (
      <Button size="sm" onClick={() => onActivate(release)}>
        {older ? "Roll back to this" : "Activate"}
        <span className="sr-only">{`: release ${release.id}`}</span>
      </Button>
    );
  }
  return (
    <li className="flex flex-col gap-2 px-4 py-3">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <span translate="no" title={release.id} className="mono min-w-0 truncate text-12 text-fg">
          {release.id}
        </span>
        <Badge tone={badge.tone} className="shrink-0">
          {badge.label}
        </Badge>
      </div>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <span className="flex min-w-0 items-baseline gap-2 text-12 text-fg-muted">
          {commit ? (
            <span translate="no" className="mono text-fg">
              {commit}
            </span>
          ) : null}
          {release.active && release.activated_at ? (
            <span>
              {"Serving since "}
              <RelativeTime value={release.activated_at} />
            </span>
          ) : (
            <span>
              {"Built "}
              <RelativeTime value={release.created_at} />
            </span>
          )}
        </span>
        {action ?? (!release.on_disk ? <span className="text-12 text-fg-faint">Removed from disk</span> : null)}
      </div>
    </li>
  );
}

/** Confirms switching to a release, then does it: seconds, not a job. */
function ActivateDialog({ domain, release, older, onClose }: { domain: string; release: Release | null; older: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { activateRelease } = useAppActions(domain);
  const close = (): void => {
    if (activateRelease.isPending) return;
    activateRelease.reset();
    onClose();
  };
  const commit = shortCommit(release?.commit);
  return (
    <Dialog
      open={release !== null}
      onOpenChange={(open) => {
        if (!open) close();
      }}
      size="sm"
      title={older ? "Roll back to this release?" : "Activate this release?"}
      description={
        <>
          {`${domain} switches to `}
          <span translate="no" className="mono text-12 text-fg">
            {release?.id}
          </span>
          {commit ? ` (commit ${commit})` : ""}
          {" and restarts. If it does not pass its health check, the release serving now is put back."}
        </>
      }
      footer={
        <>
          <Button disabled={activateRelease.isPending} onClick={close}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={activateRelease.isPending}
            onClick={() => {
              if (release === null) return;
              activateRelease.mutate(release.id, {
                onSuccess: () => {
                  onClose();
                  activateRelease.reset();
                },
                onSettled: () => {
                  // The switch is recorded as a deploy of its own.
                  void queryClient.invalidateQueries({ queryKey: deploymentKeys.all });
                },
              });
            }}
          >
            {older ? "Roll back" : "Activate"}
          </Button>
        </>
      }
    >
      {activateRelease.isError ? (
        <ErrorBlock live compact error={activateRelease.error} title={older ? "The rollback did not happen" : "The release was not activated"} />
      ) : null}
    </Dialog>
  );
}

function Releases({ domain, releases }: { domain: string; releases: Release[] }) {
  const [chosen, setChosen] = useState<Release | null>(null);
  const active = releases.find((release) => release.active);
  return (
    <>
      {releases.length === 0 ? (
        <p className="text-13 text-fg-muted">No release is on disk yet. The next deploy builds the first one.</p>
      ) : (
        <Panel>
          <ul aria-label={`Releases of ${domain}, newest first`} className="flex flex-col divide-y divide-border">
            {releases.map((release) => (
              <ReleaseRow key={release.id} release={release} active={active} onActivate={setChosen} />
            ))}
          </ul>
        </Panel>
      )}
      <ActivateDialog
        domain={domain}
        release={chosen}
        older={chosen !== null && active !== undefined && chosen.id < active.id}
        onClose={() => {
          setChosen(null);
        }}
      />
    </>
  );
}

function PointRow({ point, onRestore }: { point: RollbackPoint; onRestore: (point: RollbackPoint) => void }) {
  const commit = shortCommit(point.git_commit);
  return (
    <li className="flex flex-col gap-2 px-4 py-3">
      <span translate="no" title={point.id} className="mono min-w-0 truncate text-12 text-fg">
        {point.id}
      </span>
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-12 text-fg-muted">
          {commit ? (
            <span translate="no" className="mono text-fg">
              {commit}
            </span>
          ) : null}
          <RelativeTime value={point.created_at} />
          <span className="text-fg-faint">{`${point.description}, ${formatBytes(point.size_bytes)}`}</span>
        </span>
        <Button size="sm" onClick={() => onRestore(point)}>
          Roll back to this
          <span className="sr-only">{`: backup ${point.id}`}</span>
        </Button>
      </div>
    </li>
  );
}

function RestoreDialog({ domain, point, onClose }: { domain: string; point: RollbackPoint | null; onClose: () => void }) {
  const { rollbackToBackup } = useAppActions(domain);
  const close = (): void => {
    if (rollbackToBackup.isPending) return;
    rollbackToBackup.reset();
    onClose();
  };
  return (
    <Dialog
      open={point !== null}
      onOpenChange={(open) => {
        if (!open) close();
      }}
      size="sm"
      title="Roll back to this backup?"
      description={
        <>
          {`${domain} is restored from `}
          <span translate="no" className="mono text-12 text-fg">
            {point?.id}
          </span>
          {" and restarted, as a job. A backup of how it is now is taken first. Files written since the backup are replaced."}
        </>
      }
      footer={
        <>
          <Button disabled={rollbackToBackup.isPending} onClick={close}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={rollbackToBackup.isPending}
            onClick={() => {
              if (point === null) return;
              rollbackToBackup.mutate(point.id, {
                onSuccess: () => {
                  onClose();
                  rollbackToBackup.reset();
                },
              });
            }}
          >
            Roll back
          </Button>
        </>
      }
    >
      {rollbackToBackup.isError ? <ErrorBlock live compact error={rollbackToBackup.error} title="The rollback did not start" /> : null}
    </Dialog>
  );
}

function RollbackPoints({ domain }: { domain: string }) {
  const points = useQuery(rollbackPointsQuery(domain));
  const [chosen, setChosen] = useState<RollbackPoint | null>(null);
  return (
    <Section
      title="Rollback points"
      description={
        <>
          {"This app is deployed in place, so going back restores a backup. "}
          <Link to="/apps/$domain/settings" params={{ domain }} className={LINK}>
            Enable releases
          </Link>
          {" for rollbacks that take seconds."}
        </>
      }
    >
      {points.isError && points.data === undefined ? (
        <ErrorBlock compact error={points.error} title="Could not list the backups" onRetry={() => void points.refetch()} retrying={points.isRefetching} />
      ) : points.data === undefined ? (
        <ListSkeleton />
      ) : points.data.items.length === 0 ? (
        <div className="flex flex-col items-start gap-2 rounded-card border border-dashed border-border px-4 py-4">
          <ArchiveRestore aria-hidden="true" className="size-4 text-fg-faint" />
          <p className="text-13 text-pretty text-fg-muted">
            No backup of this app yet. One is taken before every update, and on the schedules set in{" "}
            <Link to="/backups" className={LINK}>
              Backups
            </Link>
            .
          </p>
        </div>
      ) : (
        <Panel>
          <ul aria-label={`Backups of ${domain}, newest first`} className="flex flex-col divide-y divide-border">
            {points.data.items.map((point) => (
              <PointRow key={point.id} point={point} onRestore={setChosen} />
            ))}
          </ul>
        </Panel>
      )}
      <RestoreDialog
        domain={domain}
        point={chosen}
        onClose={() => {
          setChosen(null);
        }}
      />
    </Section>
  );
}

/**
 * What the app can go back to. An app on releases switches to an earlier build in seconds; an
 * app deployed in place (or one the API finds in place, whatever it is listed as) restores a
 * backup, as a job.
 */
export function ReleasesSection({ domain, layout }: { domain: string; layout: string }) {
  const releases = useQuery({ ...releasesQuery(domain), enabled: layout === "releases" });
  if (layout !== "releases" || releases.data === null) return <RollbackPoints domain={domain} />;
  return (
    <Section
      title="Releases"
      description="Each deploy builds a release beside the one serving. Going back to one takes seconds."
    >
      {releases.isError && releases.data === undefined ? (
        <ErrorBlock compact error={releases.error} title="Could not list the releases" onRetry={() => void releases.refetch()} retrying={releases.isRefetching} />
      ) : releases.data === undefined ? (
        <ListSkeleton />
      ) : (
        <Releases domain={domain} releases={releases.data.items} />
      )}
    </Section>
  );
}

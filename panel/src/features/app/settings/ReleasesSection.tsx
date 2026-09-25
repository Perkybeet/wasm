import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { CircleCheck, Layers } from "lucide-react";
import { useState } from "react";

import { request } from "../../../api/client";
import { ElevationCancelledError } from "../../../api/errors";
import { appKeys, migrationPlanQuery, releasesQuery } from "../../../api/queries/apps";
import type { App, MigrationPlan } from "../../../api/queries/apps";
import type { ResponseOf } from "../../../api/client";
import { announce } from "../../../app/Announcer";
import { CommandHint } from "../../../components/page/CommandHint";
import { KeyValueList, KeyValueListSkeleton } from "../../../components/page/KeyValueList";
import { ErrorBlock } from "../../../components/page/QueryState";
import { RelativeTime } from "../../../components/page/RelativeTime";
import { Section } from "../../../components/page/Section";
import { Button } from "../../../components/ui/Button";
import { Dialog } from "../../../components/ui/Dialog";
import { Skeleton } from "../../../components/ui/Skeleton";
import { toast } from "../../../components/ui/toast";
import { formatBytes, formatCount } from "../../../lib/format";
import { reportActionError } from "../../apps/useAppActions";
import { useConfirmItsYou } from "../useDeleteApp";
import { MigrationPlanView } from "./MigrationPlanView";
import { LINK, PANEL } from "./panel";

type MigrationResult = ResponseOf<"/api/apps/{domain}/migrate", "post">;

/** What the app has on the release layout: the release serving, how many are on disk. */
function ReleasesFacts({ domain }: { domain: string }) {
  const releases = useQuery(releasesQuery(domain));
  if (releases.isPending) return <KeyValueListSkeleton rows={4} />;
  if (releases.isError) {
    return (
      <div className="py-3">
        <ErrorBlock compact error={releases.error} title="Could not list the releases" onRetry={() => void releases.refetch()} />
      </div>
    );
  }
  const items = releases.data?.items ?? [];
  const active = items.find((release) => release.active);
  const onDisk = items.filter((release) => release.on_disk).length;
  const removed = items.length - onDisk;
  return (
    <KeyValueList
      empty="None"
      items={[
        {
          label: "Serving",
          value: active ? active.id : null,
          hint: active ? (
            <>
              {active.commit ? `Commit ${active.commit}, activated ` : "Activated "}
              <RelativeTime value={active.activated_at ?? active.created_at} />
            </>
          ) : undefined,
        },
        {
          label: "On disk",
          value: `${formatCount(onDisk)} ${onDisk === 1 ? "release" : "releases"}`,
          mono: false,
          copy: false,
          hint: removed > 0 ? `${formatCount(removed)} more listed whose build failed and was removed` : "Each one can be activated in seconds",
        },
        {
          label: "Kept",
          value: "Not reported",
          mono: false,
          copy: false,
          hint: "WASM keeps the newest 5 releases unless this app was set to keep another number",
        },
      ]}
    />
  );
}

function MigrationDone({ result }: { result: MigrationResult }) {
  return (
    <div role="status" className="flex items-start gap-2.5 rounded-control border border-ok/40 bg-ok-soft px-3 py-2.5">
      <CircleCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-ok" />
      <div className="flex min-w-0 flex-col gap-0.5 text-13 text-fg">
        <p className="font-medium">
          {"Migrated to releases. Release "}
          <span translate="no" className="mono text-12">
            {result.release_id}
          </span>
          {" is serving."}
        </p>
        <p className="text-fg-muted">
          {`${formatCount(result.files_before)} files before, ${formatCount(result.files_after)} after (${formatBytes(result.bytes_after)}): nothing was deleted.`}
          {result.persistent.length > 0 ? ` Kept in shared/: ${result.persistent.join(", ")}.` : ""}
        </p>
      </div>
    </div>
  );
}

/** What the confirmation says the migration will do, from the plan being confirmed. */
export function confirmation(plan: MigrationPlan): string {
  const rewritten =
    plan.unit_rewrite && plan.site_rewrite
      ? "the unit and the site are rewritten to run from current"
      : plan.unit_rewrite
        ? "the unit is rewritten to run from current"
        : plan.site_rewrite
          ? "the site is rewritten to serve current"
          : "current points at it";
  const kept = plan.persistent.length > 0 ? `, ${plan.persistent.join(", ")} move to shared/` : "";
  return `The live tree becomes the first release${kept}, and ${rewritten}. The app restarts and must pass a health check; if it does not, everything is put back as it was. Nothing is deleted.`;
}

/**
 * Moving an in-place app onto releases. Nothing happens until the operator has read the plan,
 * which is worked out from the disk on request, and confirmed it; the migration itself is
 * undone by the backend if the app does not come up on the new layout.
 */
function MigrationCard({ app, onMigrated }: { app: App; onMigrated: (result: MigrationResult) => void }) {
  const domain = app.domain;
  const queryClient = useQueryClient();
  const confirmItsYou = useConfirmItsYou();
  const [requested, setRequested] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [done, setDone] = useState(false);
  // Once migrated the app has no plan: the backend answers 409, so it is not asked again.
  const plan = useQuery({ ...migrationPlanQuery(domain), enabled: requested && !done, refetchOnWindowFocus: false });

  const migrate = useMutation({
    mutationFn: (current: MigrationPlan) =>
      request("post", "/api/apps/{domain}/migrate", {
        params: { domain },
        // What was reviewed is what is kept; an empty list lets the backend look again.
        body: { persist: current.persistent.length > 0 ? current.persistent : null },
      }),
    onSuccess: (result) => {
      setDone(true);
      setConfirming(false);
      onMigrated(result);
      // Not the app's whole prefix: its plan would be asked for again and answer 409.
      void queryClient.invalidateQueries({ queryKey: appKeys.detail(domain), exact: true });
      void queryClient.invalidateQueries({ queryKey: appKeys.releases(domain) });
      void queryClient.invalidateQueries({ queryKey: appKeys.rollbackPoints(domain) });
      void queryClient.invalidateQueries({ queryKey: appKeys.list, exact: true });
      announce(`${domain} migrated to releases`);
      toast.success(`Migrated ${domain} to releases`);
    },
  });

  const openConfirm = (): void => {
    migrate.reset();
    confirmItsYou().then(
      () => {
        setConfirming(true);
      },
      (error: unknown) => {
        if (!(error instanceof ElevationCancelledError)) reportActionError("The migration could not start", error);
      },
    );
  };

  const close = (next: boolean): void => {
    if (!next && migrate.isPending) return;
    setConfirming(next);
  };

  return (
    <div className="flex min-w-0 flex-col gap-4 px-4 py-4">
      <div className="flex items-start gap-3">
        <Layers aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-fg-faint" />
        <div className="flex min-w-0 flex-col gap-1">
          <h3 className="text-14 font-medium text-fg">Enable releases</h3>
          <p className="max-w-[64ch] text-13 text-pretty text-fg-muted">
            This app is updated in place: builds run inside the directory the live process reads, and a rollback restores a backup.
            On releases every deploy builds in its own directory behind a health check, and going back takes seconds.
          </p>
        </div>
      </div>

      {!requested ? (
        <div className="pl-7">
          <Button onClick={() => setRequested(true)}>Plan the migration</Button>
        </div>
      ) : plan.isPending ? (
        <div aria-busy="true" className="flex flex-col gap-2 pl-7">
          <span className="text-13 text-fg-muted">Reading the app's directory…</span>
          <Skeleton className="h-10 w-full rounded-control" />
          <KeyValueListSkeleton rows={4} />
        </div>
      ) : plan.isError ? (
        <div className="pl-7">
          <ErrorBlock error={plan.error} title="Could not plan the migration" onRetry={() => void plan.refetch()} retrying={plan.isRefetching} />
        </div>
      ) : (
        <div className="flex min-w-0 flex-col gap-4 sm:pl-7">
          <MigrationPlanView plan={plan.data} />
          {migrate.isError ? (
            <ErrorBlock
              live
              error={migrate.error}
              title="The migration did not complete"
              hint="WASM put everything back as it was: the app still runs in place, from the same directory."
            />
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="primary" onClick={openConfirm}>
              Migrate to releases
            </Button>
            <Button variant="ghost" loading={plan.isRefetching} onClick={() => void plan.refetch()}>
              Plan again
            </Button>
          </div>
        </div>
      )}

      <Dialog
        open={confirming}
        onOpenChange={close}
        title={`Migrate ${domain} to releases?`}
        description={plan.data ? confirmation(plan.data) : undefined}
        footer={
          <>
            <Button disabled={migrate.isPending} onClick={() => close(false)}>
              Cancel
            </Button>
            <Button variant="primary" loading={migrate.isPending} onClick={() => plan.data && migrate.mutate(plan.data)}>
              Migrate to releases
            </Button>
          </>
        }
      >
        {migrate.isPending ? (
          <p className="text-13 text-fg-muted">Moving the tree, rewriting the unit and waiting for the health check. This takes a few seconds.</p>
        ) : migrate.isError ? (
          <ErrorBlock
            live
            compact
            error={migrate.error}
            title="The migration did not complete"
            hint="WASM put everything back as it was: the app still runs in place, from the same directory."
          />
        ) : undefined}
      </Dialog>
      <div className="sm:pl-7">
        <CommandHint command={`wasm app migrate ${domain}`} label="From a terminal" />
      </div>
    </div>
  );
}

/**
 * How the app's deploys are laid out on disk: for an app on releases, the release serving and
 * how many are kept to go back to; for one still in place, the way onto releases.
 */
export function ReleasesSection({ app }: { app: App }) {
  const [migrated, setMigrated] = useState<MigrationResult | null>(null);
  const onReleases = app.layout === "releases";

  return (
    <Section
      title="Releases"
      description={onReleases ? "Every deploy builds a release of its own; the one serving can be switched in seconds." : "How each deploy lands on disk."}
    >
      {migrated !== null ? <MigrationDone result={migrated} /> : null}
      <div className={PANEL}>
        {onReleases ? (
          <div className="px-4 py-1">
            <ReleasesFacts domain={app.domain} />
          </div>
        ) : (
          <MigrationCard app={app} onMigrated={setMigrated} />
        )}
      </div>
      {onReleases ? (
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
          <CommandHint command={`wasm releases list ${app.domain}`} label="From a terminal" />
          <Link to="/apps/$domain/deployments" params={{ domain: app.domain }} className={LINK}>
            Roll back from the Deployments tab
          </Link>
        </div>
      ) : null}
    </Section>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { GitCommitHorizontal, History } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { request } from "../../../api/client";
import { appKeys, appQuery } from "../../../api/queries/apps";
import { deploymentKeys, deploymentsQuery } from "../../../api/queries/deployments";
import type { Deployment } from "../../../api/queries/deployments";
import { isJobFinished, useFollowedJob } from "../../../api/queries/jobs";
import type { Job } from "../../../api/queries/jobs";
import { announce } from "../../../app/Announcer";
import { ErrorBlock } from "../../../components/page/QueryState";
import { Button } from "../../../components/ui/Button";
import { Dialog } from "../../../components/ui/Dialog";
import { RollbackDialog } from "../RollbackDialog";
import { shortCommit } from "./words";

const RUNNING = new Set(["queued", "running"]);

export type DeploymentActionKind = "rebuild" | "rollback";

const WORDS: Readonly<Record<DeploymentActionKind, { noun: string; failed: string }>> = {
  rebuild: { noun: "Rebuild", failed: "The rebuild failed" },
  rollback: { noun: "Rollback", failed: "The rollback failed" },
};

/**
 * Follows the job a deployment's action queued (useFollowedJob: the job's events, and a poll
 * as the guarantee) and, when the job records a deploy of its own - found by its `job_id`,
 * never by timing - opens it, so the operator lands on its build log. A job that ends without
 * one (a release activated in seconds) is said to have finished, and one that fails keeps its
 * error on screen, in its own words.
 */
function useDeploymentAction(domain: string) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const followed = useFollowedJob();
  const [kind, setKind] = useState<DeploymentActionKind | null>(null);
  const job = followed.job;
  const waiting = followed.id !== null && (job === null || !isJobFinished(job));
  const deployments = useQuery({
    ...deploymentsQuery({ domain, limit: 10 }),
    refetchInterval: waiting ? 1_000 : false,
  });
  const arrived = followed.id !== null ? deployments.data?.items.find((item) => item.job_id === followed.id) : undefined;
  const finished = job !== null && isJobFinished(job);
  const failed = finished && job.status !== "completed" ? job : null;
  const done = finished && job.status === "completed" && arrived === undefined ? job : null;

  useEffect(() => {
    if (arrived !== undefined) void navigate({ to: "/apps/$domain/deployments/$id", params: { domain, id: String(arrived.id) } });
  }, [arrived, domain, navigate]);

  // Once, when the job has ended: what it changed (the release serving, the history) is read again.
  const settledRef = useRef<string | null>(null);
  useEffect(() => {
    if (job === null || !isJobFinished(job) || settledRef.current === job.id) return;
    settledRef.current = job.id;
    void queryClient.invalidateQueries({ queryKey: deploymentKeys.all });
    void queryClient.invalidateQueries({ queryKey: appKeys.detail(domain) });
  }, [job, domain, queryClient]);

  return {
    kind,
    busy: waiting || (arrived === undefined && followed.id !== null && !finished),
    failed,
    done,
    follow: (next: DeploymentActionKind, queued: Job) => {
      setKind(next);
      announce(`${WORDS[next].noun} of ${domain} queued`);
      followed.follow(queued);
    },
    dismiss: () => {
      followed.dismiss();
      setKind(null);
    },
  };
}

/** The backend's reason as the end of a sentence: it has no full stop of its own. */
function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/** What rebuilding a deployment's commit does, on the app's layout. */
export function rebuildWords(layout: string, commit: string, branch: string | null): string {
  if (layout === "releases") {
    return (
      `If a release built from ${commit} is still on disk and is not the one serving, it is activated in seconds, behind the health check: nothing is built. ` +
      `Otherwise ${commit} is built into a new release, exactly as an update would, and activated behind the health check.`
    );
  }
  return (
    `A backup of the app is taken, the checkout is put on ${commit}, and the app is rebuilt and restarted. ` +
    `Untracked files such as uploads stay. The next update follows ${branch ?? "its branch"} again.`
  );
}

/** What going back to a deployment does, on the app's layout. */
export function rollbackWords(layout: string, deployment: Deployment): string {
  const id = String(deployment.id);
  if (layout === "releases") {
    return (
      `Release ${deployment.release_id ?? ""} is activated in seconds and the app restarts; if it fails its health check, the release serving now is put back. ` +
      "Nothing is rebuilt, and .env and the shared files stay as they are."
    );
  }
  return (
    `The app's files are restored from backup ${deployment.snapshot_backup ?? ""}, which holds what deployment ${id} produced, then it is rebuilt and restarted. ` +
    "A backup of the current state is taken first. Anything written to the app's directory since is replaced."
  );
}

function RebuildDialog({
  domain,
  deployment,
  layout,
  open,
  onOpenChange,
  onQueued,
}: {
  domain: string;
  deployment: Deployment;
  layout: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onQueued: (job: Job) => void;
}) {
  const commit = shortCommit(deployment.git_commit) ?? "";
  const rebuild = useMutation({
    mutationFn: () =>
      request("post", "/api/apps/{domain}/deployments/{deployment_id}/rebuild", { params: { domain, deployment_id: deployment.id } }),
    onSuccess: (accepted) => {
      onOpenChange(false);
      onQueued(accepted.job as unknown as Job);
    },
  });
  const close = (next: boolean): void => {
    if (!next && rebuild.isPending) return;
    if (!next) rebuild.reset();
    onOpenChange(next);
  };
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={`Rebuild commit ${commit}?`}
      description={rebuildWords(layout, commit, deployment.git_branch ?? null)}
      footer={
        <>
          <Button disabled={rebuild.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={rebuild.isPending} onClick={() => rebuild.mutate()}>
            Rebuild
          </Button>
        </>
      }
    >
      {rebuild.isError ? <ErrorBlock live compact error={rebuild.error} title="The rebuild did not start" /> : null}
    </Dialog>
  );
}

function RollbackToDeploymentDialog({
  domain,
  deployment,
  layout,
  open,
  onOpenChange,
  onQueued,
  onChooseAnother,
}: {
  domain: string;
  deployment: Deployment;
  layout: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onQueued: (job: Job) => void;
  onChooseAnother: () => void;
}) {
  const rollback = useMutation({
    mutationFn: () =>
      request("post", "/api/apps/{domain}/deployments/{deployment_id}/rollback", { params: { domain, deployment_id: deployment.id } }),
    onSuccess: (accepted) => {
      onOpenChange(false);
      onQueued(accepted.job as unknown as Job);
    },
  });
  const close = (next: boolean): void => {
    if (!next && rollback.isPending) return;
    if (!next) rollback.reset();
    onOpenChange(next);
  };
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={`Roll back to deployment ${String(deployment.id)}?`}
      description={rollbackWords(layout, deployment)}
      footer={
        <>
          <Button variant="ghost" disabled={rollback.isPending} onClick={onChooseAnother} className="sm:mr-auto">
            Choose another version
          </Button>
          <Button disabled={rollback.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={rollback.isPending} onClick={() => rollback.mutate()}>
            Roll back
          </Button>
        </>
      }
    >
      {rollback.isError ? <ErrorBlock live compact error={rollback.error} title="The rollback did not start" /> : null}
    </Dialog>
  );
}

/**
 * What can be done from one deploy's page: go back to what it produced, when that is still
 * possible (`rollback_available`, decided by the backend), or else say why and offer the
 * other versions; and deploy its exact commit again. The header's Update stays the page's one
 * primary action, so both are secondary here.
 */
export function DeploymentActions({ domain, deployment }: { domain: string; deployment: Deployment }) {
  const app = useQuery(appQuery(domain));
  const action = useDeploymentAction(domain);
  const [dialog, setDialog] = useState<"rebuild" | "rollback" | "chooser" | null>(null);
  const running = RUNNING.has(deployment.status);
  const layout = app.data?.layout ?? null;
  const commit = shortCommit(deployment.git_commit);
  const available = deployment.rollback_available;
  const reason = deployment.rollback_unavailable_reason ?? null;

  const queued = (kind: DeploymentActionKind) => (job: Job) => {
    action.follow(kind, job);
  };
  const setOpen = (which: typeof dialog) => (open: boolean) => {
    setDialog(open ? which : null);
  };

  if (running || layout === null) return null;
  return (
    <div className="flex max-w-md flex-col items-end gap-2">
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button
          icon={<History aria-hidden="true" />}
          disabled={action.busy}
          loading={action.busy && action.kind === "rollback"}
          onClick={() => {
            action.dismiss();
            setDialog(available ? "rollback" : "chooser");
          }}
        >
          {available ? "Roll back to this" : "Roll back…"}
        </Button>
        {commit !== null ? (
          <Button
            icon={<GitCommitHorizontal aria-hidden="true" />}
            disabled={action.busy}
            loading={action.busy && action.kind === "rebuild"}
            onClick={() => {
              action.dismiss();
              setDialog("rebuild");
            }}
          >
            Rebuild this commit
          </Button>
        ) : null}
      </div>
      {!available && reason !== null ? (
        <p className="text-right text-12 text-pretty text-fg-muted">{`Can't roll back to this deployment: ${sentence(reason)}`}</p>
      ) : null}
      {action.failed !== null && action.kind !== null ? (
        <ErrorBlock
          live
          compact
          className="text-left"
          error={{ detail: action.failed.error ?? "The job failed without saying why. Its log is on the Activity page." }}
          title={WORDS[action.kind].failed}
        />
      ) : action.done !== null && action.kind !== null ? (
        <p role="status" className="text-right text-12 text-fg-muted">
          {action.kind === "rollback"
            ? `Rolled back to deployment ${String(deployment.id)}.`
            : `Rebuilt commit ${commit ?? ""}: it is live.`}
        </p>
      ) : null}

      {commit !== null ? (
        <RebuildDialog
          domain={domain}
          deployment={deployment}
          layout={layout}
          open={dialog === "rebuild"}
          onOpenChange={setOpen("rebuild")}
          onQueued={queued("rebuild")}
        />
      ) : null}
      {available ? (
        <RollbackToDeploymentDialog
          domain={domain}
          deployment={deployment}
          layout={layout}
          open={dialog === "rollback"}
          onOpenChange={setOpen("rollback")}
          onQueued={queued("rollback")}
          onChooseAnother={() => setDialog("chooser")}
        />
      ) : null}
      <RollbackDialog domain={domain} layout={layout} open={dialog === "chooser"} onOpenChange={setOpen("chooser")} onJobQueued={queued("rollback")} />
    </div>
  );
}

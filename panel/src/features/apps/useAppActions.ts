import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import { ElevationCancelledError } from "../../api/errors";
import { appKeys } from "../../api/queries/apps";
import { jobKeys } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";
import { announce } from "../../app/Announcer";
import { toast } from "../../components/ui/toast";
import { describeError } from "../../lib/errors";

export interface AppActionOptions {
  /**
   * Called with a job the API queued (update, rollback, delete). The app page tracks it in
   * its header; without one, a toast says the job was queued.
   */
  onJobQueued?: (job: Job) => void;
}

/**
 * Reports a failed action in the toast queue: what failed, the fix, the system's words. A
 * cancelled "Confirm it's you" is not a failure, and is said as such.
 */
export function reportActionError(title: string, error: unknown): void {
  if (error instanceof ElevationCancelledError) {
    toast.info(error.detail);
    return;
  }
  const { hint, detail } = describeError(error);
  toast.error(title, { detail, ...(hint !== null ? { description: hint } : {}) });
}

type UnitVerb = "restart" | "start" | "stop";

const UNIT_WORDS: Record<UnitVerb, { past: string; noun: string }> = {
  restart: { past: "Restarted", noun: "Restart" },
  start: { past: "Started", noun: "Start" },
  stop: { past: "Stopped", noun: "Stop" },
};

function callUnit(domain: string, verb: UnitVerb) {
  const params = { params: { domain } };
  switch (verb) {
    case "restart":
      return request("post", "/api/apps/{domain}/restart", params);
    case "start":
      return request("post", "/api/apps/{domain}/start", params);
    case "stop":
      return request("post", "/api/apps/{domain}/stop", params);
  }
}

/** One synchronous systemctl verb on the app's unit, reported in a toast either way. */
function useUnitAction(domain: string, verb: UnitVerb, refresh: () => void) {
  const { past, noun } = UNIT_WORDS[verb];
  return useMutation({
    mutationFn: () => callUnit(domain, verb),
    onSuccess: () => {
      toast.success(`${past} ${domain}`);
      refresh();
    },
    onError: (error) => {
      reportActionError(`${noun} of ${domain} failed`, error);
      refresh();
    },
  });
}

/**
 * The actions on one application, each the one API call that does it: restart, start and
 * stop the unit, queue an update, a rollback or the deletion. Elevation ("Confirm it's you")
 * is handled by the API client, never here.
 *
 * Synchronous unit actions refresh the app when they return (the `app` server event does too;
 * whichever lands first wins). Queued jobs report their outcome through the `job` and
 * `notice` events, which the shell already turns into a toast, so nothing here repeats it.
 */
export function useAppActions(domain: string, { onJobQueued }: AppActionOptions = {}) {
  const queryClient = useQueryClient();

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: appKeys.detail(domain) });
    void queryClient.invalidateQueries({ queryKey: appKeys.list, exact: true });
  };

  const queued = (job: Job, verb: string): void => {
    // The job's own events keep this entry current, and can beat the response here: a job
    // that fails in milliseconds has already said so on the stream. The response's snapshot
    // only fills an empty entry, never overwrites a newer one.
    queryClient.setQueryData<Job>(jobKeys.detail(job.id), (current) => current ?? job);
    void queryClient.invalidateQueries({ queryKey: jobKeys.active });
    announce(`${verb} of ${domain} queued`);
    if (onJobQueued) onJobQueued(job);
    else toast.info(`${verb} of ${domain} queued`, { description: "You will be told when it finishes." });
  };

  const restart = useUnitAction(domain, "restart", refresh);
  const start = useUnitAction(domain, "start", refresh);
  const stop = useUnitAction(domain, "stop", refresh);

  const update = useMutation({
    mutationFn: () => request("post", "/api/jobs/update", { body: { domain } }),
    onSuccess: (result) => {
      queued(result.job, "Update");
    },
    onError: (error) => {
      reportActionError(`Update of ${domain} could not be queued`, error);
    },
  });

  const rollbackToBackup = useMutation({
    mutationFn: (backupId: string) => request("post", "/api/jobs/rollback", { body: { domain, backup_id: backupId } }),
    onSuccess: (result) => {
      queued(result.job, "Rollback");
    },
  });

  const activateRelease = useMutation({
    mutationFn: (releaseId: string) =>
      request("post", "/api/apps/{domain}/releases/{release_id}/activate", { params: { domain, release_id: releaseId } }),
    onSuccess: (result) => {
      refresh();
      if (result.rolled_back) {
        toast.error(`Release ${result.release_id} failed its health check`, {
          description: `${domain} was returned to ${result.previous_id ?? "the previous release"}.`,
        });
      } else {
        toast.success(`Activated release ${result.release_id} of ${domain}`);
      }
    },
  });

  const remove = useMutation({
    mutationFn: () => request("post", "/api/jobs/delete", { body: { domain, remove_files: true, remove_ssl: true } }),
    onSuccess: (result) => {
      queued(result.job, "Deletion");
    },
  });

  return { restart, start, stop, update, rollbackToBackup, activateRelease, remove };
}

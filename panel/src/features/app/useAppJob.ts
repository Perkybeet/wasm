import { useQuery } from "@tanstack/react-query";

import { activeJobsQuery, useFollowedJob } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";

/** How a job on an app is named while it runs and when it ends (the backend's JobType). */
const JOB_WORDS: Readonly<Record<string, { running: string; noun: string }>> = {
  deploy: { running: "Deploying", noun: "Deploy" },
  update: { running: "Updating", noun: "Update" },
  restore: { running: "Rolling back", noun: "Rollback" },
  delete: { running: "Deleting", noun: "Deletion" },
  backup: { running: "Backing up", noun: "Backup" },
  migrate: { running: "Migrating", noun: "Migration" },
  service_action: { running: "Working", noun: "Service action" },
};

export function jobWords(type: string): { running: string; noun: string } {
  return JOB_WORDS[type] ?? { running: "Working", noun: "Job" };
}

const RUNNING = new Set(["pending", "running"]);

/** The newest line the job logged, which is what it is doing now. */
export function jobStep(job: Job): string | null {
  const message = job.logs?.at(-1)?.["message"];
  return typeof message === "string" && message.trim() !== "" ? message.trim() : null;
}

export interface AppJob {
  /** A job on this app that is queued or running, from here or from anywhere else. */
  running: Job | null;
  /** The job started from this page, once it has failed, until dismissed. */
  failed: Job | null;
  /** Follows a job this page queued. */
  track: (job: Job) => void;
  dismiss: () => void;
}

/**
 * What is being done to one app right now. Any running job on it counts (a deploy from the
 * CLI, an update from another tab: the active jobs list names them); the job this page queued
 * is followed to its end (useFollowedJob: events, and a poll as the guarantee), so its failure
 * stays on screen until the operator dismisses it.
 */
export function useAppJob(domain: string): AppJob {
  const active = useQuery(activeJobsQuery());
  const followed = useFollowedJob();

  const mine = followed.job ?? undefined;
  const elsewhere = active.data?.jobs.find((job) => job.metadata?.["domain"] === domain && RUNNING.has(job.status));
  const running = mine !== undefined && RUNNING.has(mine.status) ? mine : (elsewhere ?? null);

  return {
    running,
    failed: mine?.status === "failed" ? mine : null,
    track: followed.follow,
    dismiss: followed.dismiss,
  };
}

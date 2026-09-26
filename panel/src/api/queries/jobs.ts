import { queryOptions, replaceEqualDeep, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type JobList = ResponseOf<"/api/jobs", "get">;
export type Job = ResponseOf<"/api/jobs/{job_id}", "get">;
export type JobLog = ResponseOf<"/api/jobs/{job_id}/log", "get">;

export interface JobFilters {
  status?: string;
  domain?: string;
  limit?: number;
}

export const jobKeys = {
  all: ["jobs"] as const,
  list: (filters: JobFilters) => ["jobs", "list", filters] as const,
  active: ["jobs", "active"] as const,
  /** Every single job's entries (and their logs): the prefix of `detail`. */
  details: ["job"] as const,
  detail: (id: string) => ["job", id] as const,
  log: (id: string, tail: number | null) => ["job", id, "log", { tail }] as const,
};

export const jobsQuery = (filters: JobFilters = {}) =>
  queryOptions({
    queryKey: jobKeys.list(filters),
    queryFn: ({ signal }) => request("get", "/api/jobs", { query: filters, signal }),
  });

export const activeJobsQuery = () =>
  queryOptions({
    queryKey: jobKeys.active,
    queryFn: ({ signal }) => request("get", "/api/jobs/active", { signal }),
  });

const FINISHED = new Set(["completed", "failed", "cancelled"]);

/** Whether a job has ended, one way or another: nothing about it changes after this. */
export function isJobFinished(job: Pick<Job, "status">): boolean {
  return FINISHED.has(job.status);
}

/** How often a job that has not ended is read again, whatever the events say. */
export const JOB_FOLLOW_POLL_MS = 3_000;

/** When to read a followed job again: every few seconds until it has ended, then never. */
export function jobFollowInterval(job: Pick<Job, "status"> | undefined): number | false {
  return job === undefined || !isJobFinished(job) ? JOB_FOLLOW_POLL_MS : false;
}

function isJobLike(value: unknown): value is Pick<Job, "id" | "status"> {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { id?: unknown }).id === "string" &&
    typeof (value as { status?: unknown }).status === "string"
  );
}

/**
 * How a new answer about a job replaces the one in the cache: as usual, except that a job that
 * has ended never goes back to running. Two writers race for the entry - the `job` event and a
 * fetch - and a fetch answered just before the job ended can land after its last event; taken
 * as is, it would leave the job "running" for good, because no event is left to correct it.
 * Every write goes through here: fetches, events (setQueryData) and snapshots alike.
 */
export function keepFinishedJob(previous: unknown, next: unknown): unknown {
  if (isJobLike(previous) && isJobLike(next) && previous.id === next.id && isJobFinished(previous) && !isJobFinished(next)) {
    return previous;
  }
  return replaceEqualDeep(previous, next);
}

/**
 * One job, followed to its end. The `job` events are the fast path; reading it again every few
 * seconds until it ends is the guarantee, for the event a dropped stream never delivered.
 */
export const jobQuery = (id: string) =>
  queryOptions({
    queryKey: jobKeys.detail(id),
    queryFn: ({ signal }) => request("get", "/api/jobs/{job_id}", { params: { job_id: id }, signal }),
    structuralSharing: keepFinishedJob,
    refetchInterval: (query) => jobFollowInterval(query.state.data),
  });

export interface FollowedJob {
  /** The job being followed, as last reported; null before the first answer or when none is. */
  job: Job | null;
  /** The id being followed, known before the job itself is. */
  id: string | null;
  /** Follows a job: a snapshot from the response that queued it, or only its id. */
  follow: (job: Job | string) => void;
  /** Stops following it, and forgets how it ended. */
  dismiss: () => void;
}

/**
 * Follows one job a page queued, to its end. The snapshot from the response that queued it
 * only fills an empty cache entry, never overwrites a newer one (the job's first event can
 * arrive before the response does).
 */
export function useFollowedJob(): FollowedJob {
  const queryClient = useQueryClient();
  const [id, setId] = useState<string | null>(null);
  const query = useQuery({ ...jobQuery(id ?? ""), enabled: id !== null });
  return {
    id,
    job: id === null ? null : (query.data ?? null),
    follow: (job) => {
      if (typeof job === "string") {
        setId(job);
        return;
      }
      queryClient.setQueryData<Job>(jobKeys.detail(job.id), (current) => current ?? job);
      void queryClient.invalidateQueries({ queryKey: jobKeys.active });
      setId(job.id);
    },
    dismiss: () => {
      setId(null);
    },
  };
}

/**
 * A job's captured log, one `[stamp] [LEVEL] message` line per step it reported. Read from the
 * file the job manager wrote, so it is still there after the job ended or the panel restarted.
 */
export const jobLogQuery = (id: string, tail: number | null = null) =>
  queryOptions({
    queryKey: jobKeys.log(id, tail),
    queryFn: ({ signal }) =>
      request("get", "/api/jobs/{job_id}/log", { params: { job_id: id }, query: tail === null ? {} : { tail }, signal }),
  });

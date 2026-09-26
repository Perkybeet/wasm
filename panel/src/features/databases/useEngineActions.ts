/**
 * Actions on a database engine as a whole: install (a job, since it drives the distribution
 * package manager), uninstall, start, stop, restart. Mirrors `features/apps/useAppActions.ts`:
 * one mutation per API call, elevation handled by the API client, outcomes reported in a toast.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { request } from "../../api/client";
import { databaseKeys } from "../../api/queries/databases";
import { activeJobsQuery, isJobFinished, jobKeys, useFollowedJob } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";

const RUNNING = new Set(["pending", "running"]);

/**
 * The install job running on one engine, from here or from anywhere else - the same shape
 * `features/app/useAppJob.ts` tracks a deploy with, keyed by engine instead of domain, and
 * followed to its end by the shared useFollowedJob. An install changes the engines list itself
 * (installed, a version, a port to reach), which nothing else invalidates once the job
 * settles, so this refreshes it directly.
 */
export function useEngineJob(engine: string) {
  const queryClient = useQueryClient();
  const active = useQuery(activeJobsQuery());
  const followed = useFollowedJob();

  const mine = followed.job ?? undefined;
  const elsewhere = active.data?.jobs.find((job) => job.metadata?.["engine"] === engine && RUNNING.has(job.status));
  const running = mine !== undefined && RUNNING.has(mine.status) ? mine : (elsewhere ?? null);

  const ended = mine !== undefined && isJobFinished(mine);
  useEffect(() => {
    if (ended) void queryClient.invalidateQueries({ queryKey: databaseKeys.engines });
  }, [ended, queryClient]);

  return { running, track: (job: Job): void => followed.follow(job) };
}

export function useEngineActions() {
  const queryClient = useQueryClient();

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: databaseKeys.engines });
  };

  const install = useMutation({
    mutationFn: (engine: string) => request("post", "/api/databases/engines/{engine}/install", { params: { engine } }),
    onSuccess: (result) => {
      queryClient.setQueryData<Job>(jobKeys.detail(result.job_id), (current) => current ?? (result.job as Job));
      void queryClient.invalidateQueries({ queryKey: jobKeys.active });
      toast.info(result.message, { description: "You will be told when it finishes." });
    },
    onError: (error, engine) => {
      reportActionError(`Could not queue the installation of ${engine}`, error);
    },
  });

  const start = useMutation({
    mutationFn: (engine: string) => request("post", "/api/databases/engines/{engine}/start", { params: { engine } }),
    onSuccess: (result) => {
      toast.success(result.message);
      refresh();
    },
    onError: (error, engine) => {
      reportActionError(`Could not start ${engine}`, error);
    },
  });

  const stop = useMutation({
    mutationFn: (engine: string) => request("post", "/api/databases/engines/{engine}/stop", { params: { engine } }),
    onSuccess: (result) => {
      toast.success(result.message);
      refresh();
    },
    onError: (error, engine) => {
      reportActionError(`Could not stop ${engine}`, error);
    },
  });

  const restart = useMutation({
    mutationFn: (engine: string) => request("post", "/api/databases/engines/{engine}/restart", { params: { engine } }),
    onSuccess: (result) => {
      toast.success(result.message);
      refresh();
    },
    onError: (error, engine) => {
      reportActionError(`Could not restart ${engine}`, error);
    },
  });

  return { install, start, stop, restart };
}

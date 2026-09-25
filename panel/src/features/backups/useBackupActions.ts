/**
 * Actions on backups and their schedules: one mutation per API call, matching
 * `features/apps/useAppActions.ts`. Creating and restoring a backup are jobs (both tar or
 * untar a whole application tree); verifying, deleting and scheduling answer immediately.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import { backupKeys } from "../../api/queries/backups";
import { jobKeys } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";

export interface CreateBackupInput {
  domain: string;
  description: string;
  includeEnv: boolean;
  includeNodeModules: boolean;
  includeBuild: boolean;
  includeDatabase: boolean;
  includeDockerVolumes: boolean;
  redisMethod: string;
  tags: string[];
}

export interface RestoreBackupInput {
  backupId: string;
  targetDomain?: string | undefined;
  restoreEnv: boolean;
  verify: boolean;
}

export interface CreateScheduleInput {
  domain: string;
  schedule: string;
  retentionCount: number;
  retentionDays: number;
  includeDatabases: boolean;
}

export function useBackupActions() {
  const queryClient = useQueryClient();

  const refreshList = (): void => {
    void queryClient.invalidateQueries({ queryKey: backupKeys.all });
    void queryClient.invalidateQueries({ queryKey: backupKeys.storage });
  };

  const queueJob = (result: { job_id: string; job?: unknown; message: string }, verb: string): void => {
    queryClient.setQueryData<Job>(jobKeys.detail(result.job_id), (current) => current ?? (result.job as Job));
    void queryClient.invalidateQueries({ queryKey: jobKeys.active });
    toast.info(result.message, { description: `You will be told when the ${verb} finishes.` });
  };

  const create = useMutation({
    mutationFn: (input: CreateBackupInput) =>
      request("post", "/api/backups", {
        body: {
          domain: input.domain,
          description: input.description,
          include_env: input.includeEnv,
          include_node_modules: input.includeNodeModules,
          include_build: input.includeBuild,
          include_database: input.includeDatabase,
          include_docker_volumes: input.includeDockerVolumes,
          schemas: [],
          redis_method: input.redisMethod,
          tags: input.tags,
        },
      }),
    onSuccess: (result) => {
      queueJob(result, "backup");
    },
    onError: (error, input) => {
      reportActionError(`Could not queue a backup of ${input.domain}`, error);
    },
  });

  const verify = useMutation({
    mutationFn: (backupId: string) => request("post", "/api/backups/{backup_id}/verify", { params: { backup_id: backupId } }),
  });

  const restore = useMutation({
    mutationFn: (input: RestoreBackupInput) =>
      request("post", "/api/backups/{backup_id}/restore", {
        params: { backup_id: input.backupId },
        body: { target_domain: input.targetDomain ?? null, restore_env: input.restoreEnv, verify: input.verify },
      }),
    onSuccess: (result) => {
      queueJob(result, "restore");
    },
    onError: (error) => {
      reportActionError("Could not queue the restore", error);
    },
  });

  const remove = useMutation({
    mutationFn: (backupId: string) => request("delete", "/api/backups/{backup_id}", { params: { backup_id: backupId } }),
    onSuccess: (result) => {
      toast.success(result.message);
      refreshList();
    },
    onError: (error) => {
      reportActionError("Could not delete the backup", error);
    },
  });

  const createSchedule = useMutation({
    mutationFn: (input: CreateScheduleInput) =>
      request("post", "/api/backup-schedules", {
        body: {
          domain: input.domain,
          schedule: input.schedule,
          retention_count: input.retentionCount,
          retention_days: input.retentionDays,
          include_databases: input.includeDatabases,
        },
      }),
    onSuccess: (result) => {
      toast.success(result.message);
      void queryClient.invalidateQueries({ queryKey: backupKeys.schedules });
    },
  });

  const deleteSchedule = useMutation({
    mutationFn: (domain: string) => request("delete", "/api/backup-schedules/{domain}", { params: { domain } }),
    onSuccess: (result) => {
      toast.success(result.message);
      void queryClient.invalidateQueries({ queryKey: backupKeys.schedules });
    },
    onError: (error) => {
      reportActionError("Could not remove the schedule", error);
    },
  });

  return { create, verify, restore, remove, createSchedule, deleteSchedule };
}

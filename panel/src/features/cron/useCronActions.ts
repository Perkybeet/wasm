import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import type { BodyOf } from "../../api/client";
import { cronKeys } from "../../api/queries/cron";
import { reportActionError } from "../apps/useAppActions";
import { toast } from "../../components/ui/toast";

export type CreateCronJobBody = BodyOf<"/api/cron", "post">;

/**
 * Every action on a cron job: creating (or rewriting) one, running it now, enabling and
 * disabling its timer, and deleting it. All but create/delete answer immediately, from a
 * synchronous systemctl call.
 */
export function useCronActions() {
  const queryClient = useQueryClient();

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: cronKeys.all });
  };

  const create = useMutation({
    mutationFn: (body: CreateCronJobBody) => request("post", "/api/cron", { body }),
    onSuccess: (result) => {
      toast.success(`Created ${result.job?.name ?? "the job"}`, result.job ? { description: `Next run: ${result.job.next_run}.` } : {});
      refresh();
    },
  });

  const remove = useMutation({
    mutationFn: (name: string) => request("delete", "/api/cron/{name}", { params: { name } }),
    onSuccess: (_result, name) => {
      toast.success(`Deleted ${name}`);
      refresh();
    },
    onError: (error, name) => {
      reportActionError(`Deletion of ${name} failed`, error);
    },
  });

  const run = useMutation({
    mutationFn: (name: string) => request("post", "/api/cron/{name}/run", { params: { name } }),
    onSuccess: (_result, name) => {
      toast.success(`Started ${name}`, { description: "Its result will appear in its run history shortly." });
    },
    onError: (error, name) => {
      reportActionError(`Could not start ${name}`, error);
    },
  });

  const enable = useMutation({
    mutationFn: (name: string) => request("post", "/api/cron/{name}/enable", { params: { name } }),
    onSuccess: (_result, name) => {
      toast.success(`Enabled ${name}`);
      refresh();
    },
    onError: (error, name) => {
      reportActionError(`Could not enable ${name}`, error);
      refresh();
    },
  });

  const disable = useMutation({
    mutationFn: (name: string) => request("post", "/api/cron/{name}/disable", { params: { name } }),
    onSuccess: (_result, name) => {
      toast.success(`Disabled ${name}`);
      refresh();
    },
    onError: (error, name) => {
      reportActionError(`Could not disable ${name}`, error);
      refresh();
    },
  });

  return { create, remove, run, enable, disable };
}

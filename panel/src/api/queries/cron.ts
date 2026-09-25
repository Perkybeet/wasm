import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type CronJobList = ResponseOf<"/api/cron", "get">;
export type CronRuns = ResponseOf<"/api/cron/{name}/runs", "get">;

export const cronKeys = {
  all: ["cron"] as const,
  runs: (name: string) => ["cron", name, "runs"] as const,
};

export const cronJobsQuery = () =>
  queryOptions({
    queryKey: cronKeys.all,
    queryFn: ({ signal }) => request("get", "/api/cron", { signal }),
  });

export const cronRunsQuery = (name: string, limit = 20) =>
  queryOptions({
    queryKey: cronKeys.runs(name),
    queryFn: ({ signal }) => request("get", "/api/cron/{name}/runs", { params: { name }, query: { limit }, signal }),
  });

import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type CronJobList = ResponseOf<"/api/cron", "get">;
export type CronRuns = ResponseOf<"/api/cron/{name}/runs", "get">;
export type CronPreview = ResponseOf<"/api/cron/preview", "post">;

export const cronKeys = {
  all: ["cron"] as const,
  runs: (name: string) => ["cron", name, "runs"] as const,
  preview: (schedule: string) => ["cron", "preview", schedule] as const,
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

/**
 * The normalised calendar and next runs for a schedule, keyed by that exact schedule string:
 * a caller that debounces its input and passes the debounced value gets a query per distinct
 * value, so an in-flight request for a schedule the operator has since changed cannot resolve
 * after a newer one and overwrite it - TanStack Query keeps each key's result separate.
 */
export const cronPreviewQuery = (schedule: string) =>
  queryOptions({
    queryKey: cronKeys.preview(schedule),
    queryFn: ({ signal }) => request("post", "/api/cron/preview", { body: { schedule }, signal }),
  });

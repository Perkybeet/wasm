import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type JobList = ResponseOf<"/api/jobs", "get">;
export type Job = ResponseOf<"/api/jobs/{job_id}", "get">;

export interface JobFilters {
  status?: string;
  domain?: string;
  limit?: number;
}

export const jobKeys = {
  all: ["jobs"] as const,
  list: (filters: JobFilters) => ["jobs", "list", filters] as const,
  active: ["jobs", "active"] as const,
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

export const jobQuery = (id: string) =>
  queryOptions({
    queryKey: jobKeys.detail(id),
    queryFn: ({ signal }) => request("get", "/api/jobs/{job_id}", { params: { job_id: id }, signal }),
  });

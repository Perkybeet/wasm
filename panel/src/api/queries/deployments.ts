import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { QueryOf, ResponseOf } from "../client";

export type DeploymentList = ResponseOf<"/api/deployments", "get">;
export type Deployment = ResponseOf<"/api/deployments/{deployment_id}", "get">;
export type DeploymentLog = ResponseOf<"/api/deployments/{deployment_id}/log", "get">;

/** Filters of the history list, exactly as the endpoint takes them (keyset: `before_id`). */
export type DeploymentFilters = NonNullable<QueryOf<"/api/deployments", "get">>;

/**
 * Keys of the deployment history. The realtime layer invalidates `all` whenever a job changes
 * status, so every list and detail refreshes when a deploy starts or ends.
 */
export const deploymentKeys = {
  all: ["deployments"] as const,
  list: (filters: DeploymentFilters) => ["deployments", "list", filters] as const,
  pages: (filters: Omit<DeploymentFilters, "before_id">) => ["deployments", "pages", filters] as const,
  detail: (id: number) => ["deployments", "detail", id] as const,
  log: (id: number, tail: number | null) => ["deployments", "detail", id, "log", { tail }] as const,
};

export const deploymentsQuery = (filters: DeploymentFilters = {}) =>
  queryOptions({
    queryKey: deploymentKeys.list(filters),
    queryFn: ({ signal }) => request("get", "/api/deployments", { query: filters, signal }),
  });

export const deploymentQuery = (id: number) =>
  queryOptions({
    queryKey: deploymentKeys.detail(id),
    queryFn: ({ signal }) => request("get", "/api/deployments/{deployment_id}", { params: { deployment_id: id }, signal }),
  });

export const deploymentLogQuery = (id: number, tail: number | null = null) =>
  queryOptions({
    queryKey: deploymentKeys.log(id, tail),
    queryFn: ({ signal }) =>
      request("get", "/api/deployments/{deployment_id}/log", {
        params: { deployment_id: id },
        query: tail === null ? {} : { tail },
        signal,
      }),
  });

/**
 * The history a page at a time, newest first, for a "Load more" list. Keyset pagination: each
 * page asks for the rows below the last id of the one before, so a deploy that starts while
 * the operator reads never shifts or repeats a row.
 */
export const deploymentPagesQuery = (filters: Omit<DeploymentFilters, "before_id"> = {}) =>
  infiniteQueryOptions({
    queryKey: deploymentKeys.pages(filters),
    queryFn: ({ signal, pageParam }) =>
      request("get", "/api/deployments", {
        query: pageParam === null ? filters : { ...filters, before_id: pageParam },
        signal,
      }),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_before_id ?? null,
  });

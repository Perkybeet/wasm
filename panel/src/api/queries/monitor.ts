import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type MonitorStatus = ResponseOf<"/api/monitor/status", "get">;
export type ObservationList = ResponseOf<"/api/monitor/observations", "get">;

export const monitorKeys = {
  all: ["monitor"] as const,
  status: ["monitor", "status"] as const,
  config: ["monitor", "config"] as const,
  observations: (includeAcknowledged: boolean) => ["monitor", "observations", { includeAcknowledged }] as const,
};

export const monitorStatusQuery = () =>
  queryOptions({
    queryKey: monitorKeys.status,
    queryFn: ({ signal }) => request("get", "/api/monitor/status", { signal }),
  });

export const monitorConfigQuery = () =>
  queryOptions({
    queryKey: monitorKeys.config,
    queryFn: ({ signal }) => request("get", "/api/monitor/config", { signal }),
  });

export const observationsQuery = (includeAcknowledged = false) =>
  queryOptions({
    queryKey: monitorKeys.observations(includeAcknowledged),
    queryFn: ({ signal }) =>
      request("get", "/api/monitor/observations", { query: { include_acknowledged: includeAcknowledged }, signal }),
  });

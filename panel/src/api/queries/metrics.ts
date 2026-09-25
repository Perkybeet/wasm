import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { QueryOf } from "../client";

/** The windows the history endpoint accepts. */
export type MetricWindow = NonNullable<NonNullable<QueryOf<"/api/metrics/{metric}", "get">>["window"]>;

/** The collector's newest sample, metric name to value, as the `metrics` event carries it. */
export type MetricsSnapshot = Record<string, number>;

export const metricKeys = {
  all: ["metrics"] as const,
  /** Fed only by the `metrics` server event; there is no REST read of the live sample. */
  latest: ["metrics", "latest"] as const,
  catalogue: ["metrics", "catalogue"] as const,
  series: (metric: string, window: MetricWindow) => ["metrics", "series", metric, { window }] as const,
};

export const metricsCatalogueQuery = () =>
  queryOptions({
    queryKey: metricKeys.catalogue,
    queryFn: ({ signal }) => request("get", "/api/metrics", { signal }),
  });

export const metricSeriesQuery = (metric: string, window: MetricWindow) =>
  queryOptions({
    queryKey: metricKeys.series(metric, window),
    queryFn: ({ signal }) =>
      request("get", "/api/metrics/{metric}", { params: { metric }, query: { window }, signal }),
  });

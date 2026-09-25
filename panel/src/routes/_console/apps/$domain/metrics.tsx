import { createFileRoute } from "@tanstack/react-router";

import { MetricsTab } from "../../../../features/app/metrics/MetricsTab";
import { DEFAULT_RANGE, isRange } from "../../../../features/app/metrics/ranges";
import type { MetricRange } from "../../../../features/app/metrics/ranges";

interface MetricsSearch {
  /** The charts' time range; the last 24 hours when absent. */
  range?: MetricRange;
}

function validateSearch(search: Record<string, unknown>): MetricsSearch {
  const range = search["range"];
  return isRange(range) && range !== DEFAULT_RANGE ? { range } : {};
}

export const Route = createFileRoute("/_console/apps/$domain/metrics")({
  validateSearch,
  component: AppMetricsTab,
});

function AppMetricsTab() {
  const { domain } = Route.useParams();
  const { range = DEFAULT_RANGE } = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <MetricsTab
      domain={domain}
      range={range}
      onRangeChange={(next) => void navigate({ search: next === DEFAULT_RANGE ? {} : { range: next }, replace: true })}
    />
  );
}

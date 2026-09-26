import { createFileRoute } from "@tanstack/react-router";

import type { MetricWindow } from "../../api/queries/metrics";
import { WINDOWS } from "../../features/overview/MachineCharts";
import { OverviewPage } from "../../features/overview/OverviewPage";

interface OverviewSearch {
  /** The machine charts' time range; the last hour when absent. */
  window?: MetricWindow;
}

function validateSearch(search: Record<string, unknown>): OverviewSearch {
  const window = search["window"];
  const known = WINDOWS.find((option) => option.value === window);
  return known === undefined ? {} : { window: known.value };
}

export const Route = createFileRoute("/_console/")({
  validateSearch,
  component: OverviewRoute,
});

function OverviewRoute() {
  const { window = "1h" } = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <OverviewPage
      window={window}
      onWindowChange={(next) => void navigate({ search: next === "1h" ? {} : { window: next }, replace: true })}
    />
  );
}

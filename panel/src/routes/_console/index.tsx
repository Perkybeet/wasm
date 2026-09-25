import { createFileRoute } from "@tanstack/react-router";

import type { MetricWindow } from "../../api/queries/metrics";
import { OverviewPage } from "../../features/overview/OverviewPage";

const WINDOWS: readonly MetricWindow[] = ["1h", "24h", "30d"];

interface OverviewSearch {
  /** The machine charts' time range; the last hour when absent. */
  window?: MetricWindow;
}

function validateSearch(search: Record<string, unknown>): OverviewSearch {
  const window = search["window"];
  return typeof window === "string" && (WINDOWS as readonly string[]).includes(window) ? { window: window as MetricWindow } : {};
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

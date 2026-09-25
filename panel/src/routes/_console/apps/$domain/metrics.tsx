import { createFileRoute } from "@tanstack/react-router";
import { ChartLine } from "lucide-react";

import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/metrics")({
  component: AppMetricsTab,
});

function AppMetricsTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<ChartLine />}
      title="CPU and memory over time"
      description="The last hour, day, week or month, with each deploy marked on the timeline and every chart also readable as a table."
      documentTitle={`Metrics - ${domain}`}
    />
  );
}

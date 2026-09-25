import { createFileRoute } from "@tanstack/react-router";
import { Gauge } from "lucide-react";

import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/")({
  component: AppOverviewTab,
});

function AppOverviewTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<Gauge />}
      title="Release, runtime and resources"
      description="The current release and commit, the last five deploys, domains with their certificates, the unit, port and uptime, and resource use against its limits."
      command={`wasm status ${domain}`}
      documentTitle={`Overview - ${domain}`}
    />
  );
}

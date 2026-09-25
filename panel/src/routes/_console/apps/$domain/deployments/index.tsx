import { createFileRoute } from "@tanstack/react-router";
import { Rocket } from "lucide-react";

import { Placeholder } from "../../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/deployments/")({
  component: AppDeploymentsTab,
});

function AppDeploymentsTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<Rocket />}
      title="Every deploy of this app"
      description="Status, commit, trigger, start time and duration of each deploy, newest first. Each one opens its build log."
      documentTitle={`Deployments - ${domain}`}
    />
  );
}

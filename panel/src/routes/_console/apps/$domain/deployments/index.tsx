import { createFileRoute } from "@tanstack/react-router";

import { DeploymentsTab } from "../../../../../features/app/deployments/DeploymentsTab";

export const Route = createFileRoute("/_console/apps/$domain/deployments/")({
  component: AppDeploymentsTab,
});

function AppDeploymentsTab() {
  const { domain } = Route.useParams();
  return <DeploymentsTab domain={domain} />;
}

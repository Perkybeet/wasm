import { createFileRoute } from "@tanstack/react-router";

import { DeploymentPage } from "../../../../../features/app/deployments/DeploymentPage";

export const Route = createFileRoute("/_console/apps/$domain/deployments/$id")({
  component: DeploymentRoute,
});

function DeploymentRoute() {
  const { domain, id } = Route.useParams();
  // Keyed: another deployment starts from nothing, with none of this one's waits or dialogs.
  return <DeploymentPage key={id} domain={domain} id={id} />;
}

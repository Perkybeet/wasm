import { createFileRoute } from "@tanstack/react-router";
import { Rocket } from "lucide-react";

import { Placeholder } from "../../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/deployments/$id")({
  component: DeploymentPage,
});

function DeploymentPage() {
  const { domain, id } = Route.useParams();
  return (
    <Placeholder
      icon={<Rocket />}
      title={`Deployment ${id}`}
      description="Each phase from fetch to health check, the build log streamed live while it runs, and the error verbatim with its fix if it failed."
      documentTitle={`Deployment ${id} - ${domain}`}
    />
  );
}

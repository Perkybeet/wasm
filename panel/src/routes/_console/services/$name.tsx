import { createFileRoute } from "@tanstack/react-router";

import { ServiceDetailPage } from "../../../features/services/ServiceDetailPage";

export const Route = createFileRoute("/_console/services/$name")({
  component: ServiceRoute,
});

function ServiceRoute() {
  const { name } = Route.useParams();
  return <ServiceDetailPage name={name} />;
}

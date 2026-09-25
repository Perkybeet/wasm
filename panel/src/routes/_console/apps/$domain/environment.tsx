import { createFileRoute } from "@tanstack/react-router";

import { EnvironmentTab } from "../../../../features/app/environment/EnvironmentTab";

export const Route = createFileRoute("/_console/apps/$domain/environment")({
  component: AppEnvironmentTab,
});

function AppEnvironmentTab() {
  const { domain } = Route.useParams();
  return <EnvironmentTab domain={domain} />;
}

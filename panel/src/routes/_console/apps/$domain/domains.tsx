import { createFileRoute } from "@tanstack/react-router";

import { AppDomainsTab } from "../../../../features/domains/AppDomainsTab";

/** The names an application answers on, and what its certificate does for each. */
export const Route = createFileRoute("/_console/apps/$domain/domains")({
  component: AppDomainsRoute,
});

function AppDomainsRoute() {
  const { domain } = Route.useParams();
  return <AppDomainsTab domain={domain} />;
}

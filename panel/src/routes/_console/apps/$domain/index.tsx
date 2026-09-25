import { createFileRoute } from "@tanstack/react-router";

import { AppOverview } from "../../../../features/app/AppOverview";

export const Route = createFileRoute("/_console/apps/$domain/")({
  component: AppOverviewTab,
});

function AppOverviewTab() {
  const { domain } = Route.useParams();
  return <AppOverview domain={domain} />;
}

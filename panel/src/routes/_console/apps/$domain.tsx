import { createFileRoute } from "@tanstack/react-router";

import { AppLayout } from "../../../features/app/AppLayout";

/** One application: its header and the tabs that divide it. Each tab is a URL. */
export const Route = createFileRoute("/_console/apps/$domain")({
  component: AppRoute,
});

function AppRoute() {
  const { domain } = Route.useParams();
  // Keyed: another app is another page, with none of this one's tracked job or dialogs.
  return <AppLayout key={domain} domain={domain} />;
}

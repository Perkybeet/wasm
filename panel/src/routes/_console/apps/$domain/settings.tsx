import { createFileRoute } from "@tanstack/react-router";

import { SettingsTab } from "../../../../features/app/settings/SettingsTab";

export const Route = createFileRoute("/_console/apps/$domain/settings")({
  component: AppSettingsTab,
});

function AppSettingsTab() {
  const { domain } = Route.useParams();
  return <SettingsTab domain={domain} />;
}

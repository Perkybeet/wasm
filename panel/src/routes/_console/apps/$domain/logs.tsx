import { createFileRoute } from "@tanstack/react-router";

import { LogsTab } from "../../../../features/app/logs/LogsTab";

export const Route = createFileRoute("/_console/apps/$domain/logs")({
  component: AppLogsTab,
});

function AppLogsTab() {
  const { domain } = Route.useParams();
  // Keyed: another app's journal is another stream, from its first line.
  return <LogsTab key={domain} domain={domain} />;
}

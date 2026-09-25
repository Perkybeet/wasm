import { createFileRoute } from "@tanstack/react-router";
import { ScrollText } from "lucide-react";

import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/logs")({
  component: AppLogsTab,
});

function AppLogsTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<ScrollText />}
      title="Live output of the app's service"
      description="The systemd journal as it is written: follow, pause, search and download the last lines."
      command={`wasm logs ${domain}`}
      documentTitle={`Logs - ${domain}`}
    />
  );
}

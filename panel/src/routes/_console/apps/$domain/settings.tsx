import { createFileRoute } from "@tanstack/react-router";
import { Wrench } from "lucide-react";

import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/settings")({
  component: AppSettingsTab,
});

function AppSettingsTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<Wrench />}
      title="Source, build and runtime"
      description="Repository and branch, build and start commands, port, the deploy webhook, and deleting the app."
      documentTitle={`Settings - ${domain}`}
    />
  );
}

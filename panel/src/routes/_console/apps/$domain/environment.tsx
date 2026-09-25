import { createFileRoute } from "@tanstack/react-router";
import { Variable } from "lucide-react";

import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/environment")({
  component: AppEnvironmentTab,
});

function AppEnvironmentTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<Variable />}
      title="Environment variables"
      description="Masked until revealed one at a time, edited in place or pasted as a .env file, with the changes reviewed before they are saved."
      command={`wasm env show ${domain}`}
      documentTitle={`Environment - ${domain}`}
    />
  );
}

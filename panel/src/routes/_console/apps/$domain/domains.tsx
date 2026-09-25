import { createFileRoute } from "@tanstack/react-router";
import { Globe } from "lucide-react";

import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/domains")({
  component: AppDomainsTab,
});

function AppDomainsTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<Globe />}
      title="Domains and certificate"
      description="The domains this app answers on and the state of the TLS certificate that covers them."
      command={`wasm cert info ${domain}`}
      documentTitle={`Domains - ${domain}`}
    />
  );
}

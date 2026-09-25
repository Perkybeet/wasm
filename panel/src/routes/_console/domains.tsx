import { createFileRoute } from "@tanstack/react-router";
import { ShieldCheck } from "lucide-react";

import { PageHeader } from "../../app/PageHeader";
import { Placeholder } from "../../app/Placeholder";

export const Route = createFileRoute("/_console/domains")({
  component: DomainsPage,
});

function DomainsPage() {
  return (
    <>
      <PageHeader
        title="Domains and certificates"
        description="TLS certificates and the web server sites that serve each domain."
      />
      <Placeholder
        icon={<ShieldCheck />}
        title="Certificates and sites"
        description="Each certificate with its domains and expiry, renewal and revocation, and every nginx or Apache site with a configuration editor that tests before it saves."
        command="wasm cert list"
      />
    </>
  );
}

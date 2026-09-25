import { createFileRoute } from "@tanstack/react-router";
import { Cog } from "lucide-react";

import { PageHeader } from "../../../app/PageHeader";
import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/services/$name")({
  component: ServicePage,
});

function ServicePage() {
  const { name } = Route.useParams();
  return (
    <>
      <PageHeader title={name} description="A systemd unit on this machine." breadcrumbs={[{ label: "Services", to: "/services" }]} />
      <Placeholder
        icon={<Cog />}
        title="State, logs and unit file"
        description="Whether the unit is running and enabled, its journal as it is written, and its unit file."
        command={`wasm service status ${name}`}
      />
    </>
  );
}

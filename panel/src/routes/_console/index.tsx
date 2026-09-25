import { createFileRoute } from "@tanstack/react-router";
import { Gauge } from "lucide-react";

import { PageHeader } from "../../app/PageHeader";
import { Placeholder } from "../../app/Placeholder";

export const Route = createFileRoute("/_console/")({
  component: OverviewPage,
});

function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="The state of this machine and everything deployed on it."
      />
      <Placeholder
        icon={<Gauge />}
        title="Problems first, then the rest"
        description="Failed apps and units, expiring certificates and monitor findings come first, followed by CPU, memory and disk charts, every application with its state, and the latest deploys."
        command="wasm health"
      />
    </>
  );
}

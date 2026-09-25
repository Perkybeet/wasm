import { createFileRoute } from "@tanstack/react-router";
import { Cog } from "lucide-react";

import { PageHeader } from "../../../app/PageHeader";
import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/services/")({
  component: ServicesPage,
});

function ServicesPage() {
  return (
    <>
      <PageHeader
        title="Services"
        description="The systemd units on this machine, including the ones WASM manages."
      />
      <Placeholder
        icon={<Cog />}
        title="Units and their state"
        description="Start, stop and restart units, follow their logs and edit unit files with a check before saving."
        command="wasm service list"
      />
    </>
  );
}

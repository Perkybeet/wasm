import { createFileRoute } from "@tanstack/react-router";
import { Server } from "lucide-react";

import { PageHeader } from "../../app/PageHeader";
import { Placeholder } from "../../app/Placeholder";

export const Route = createFileRoute("/_console/server")({
  component: ServerPage,
});

function ServerPage() {
  return (
    <>
      <PageHeader
        title="Server"
        description="Health, hardware and processes of this machine."
      />
      <Placeholder
        icon={<Server />}
        title="Health checks and system details"
        description="The checks wasm health runs, disks, network interfaces, the busiest processes and the resource monitor with its findings."
        command="wasm health"
      />
    </>
  );
}

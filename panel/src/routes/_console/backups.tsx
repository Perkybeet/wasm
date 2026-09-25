import { createFileRoute } from "@tanstack/react-router";
import { Archive } from "lucide-react";

import { PageHeader } from "../../app/PageHeader";
import { Placeholder } from "../../app/Placeholder";

export const Route = createFileRoute("/_console/backups")({
  component: BackupsPage,
});

function BackupsPage() {
  return (
    <>
      <PageHeader
        title="Backups"
        description="Snapshots of your applications, their schedules and the storage they use."
      />
      <Placeholder
        icon={<Archive />}
        title="Backups and schedules"
        description="Create, verify and restore app backups with or without their databases, and schedule them to run on their own."
        command="wasm backup list"
      />
    </>
  );
}

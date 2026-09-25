import { createFileRoute } from "@tanstack/react-router";
import { History } from "lucide-react";

import { PageHeader } from "../../app/PageHeader";
import { Placeholder } from "../../app/Placeholder";

export const Route = createFileRoute("/_console/activity")({
  component: ActivityPage,
});

function ActivityPage() {
  return (
    <>
      <PageHeader
        title="Activity"
        description="What happened on this machine, who did it and how it ended."
      />
      <Placeholder
        icon={<History />}
        title="One timeline of jobs and the audit log"
        description="Deploys, restarts, renewals, sign-ins and configuration changes, filterable by actor, action and result. Each job opens its full log."
      />
    </>
  );
}

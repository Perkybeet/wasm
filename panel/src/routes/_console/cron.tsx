import { createFileRoute } from "@tanstack/react-router";
import { Clock } from "lucide-react";

import { PageHeader } from "../../app/PageHeader";
import { Placeholder } from "../../app/Placeholder";

export const Route = createFileRoute("/_console/cron")({
  component: CronPage,
});

function CronPage() {
  return (
    <>
      <PageHeader
        title="Cron"
        description="Scheduled jobs that run commands on this machine."
      />
      <Placeholder
        icon={<Clock />}
        title="Scheduled jobs"
        description="Each job's schedule in words and as written, its next run and last result. The editor offers presets and previews the next five runs."
      />
    </>
  );
}

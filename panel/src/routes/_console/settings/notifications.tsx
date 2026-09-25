import { createFileRoute } from "@tanstack/react-router";
import { Bell } from "lucide-react";

import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/settings/notifications")({
  component: NotificationSettings,
});

function NotificationSettings() {
  return (
    <Placeholder
      icon={<Bell />}
      title="Where alerts go"
      description="Channels for deploy and monitor alerts, a test for each one, and which events are sent where."
      documentTitle="Notifications settings"
    />
  );
}

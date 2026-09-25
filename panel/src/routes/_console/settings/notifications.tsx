import { createFileRoute } from "@tanstack/react-router";

import { NotificationSettings } from "../../../features/settings/NotificationSettings";

/** Settings > Notifications: channels with a test each, the events sent, private destinations. */
export const Route = createFileRoute("/_console/settings/notifications")({
  component: NotificationSettings,
});

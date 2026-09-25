import { createFileRoute } from "@tanstack/react-router";

import { SecuritySettings } from "../../../features/settings/SecuritySettings";

/** Settings > Security: two-factor authentication, sessions and the lockout policy. */
export const Route = createFileRoute("/_console/settings/security")({
  component: SecuritySettings,
});

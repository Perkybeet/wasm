import { createFileRoute } from "@tanstack/react-router";
import { Lock } from "lucide-react";

import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/settings/security")({
  component: SecuritySettings,
});

function SecuritySettings() {
  return (
    <Placeholder
      icon={<Lock />}
      title="Sign-in and sessions"
      description="Two-factor authentication and backup codes, the sessions signed in right now, and the lockout policy."
      documentTitle="Security settings"
    />
  );
}

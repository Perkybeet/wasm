import { createFileRoute } from "@tanstack/react-router";
import { KeyRound } from "lucide-react";

import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/settings/tokens")({
  component: TokenSettings,
});

function TokenSettings() {
  return (
    <Placeholder
      icon={<KeyRound />}
      title="Tokens for automation"
      description="Named tokens with a scope and an optional expiry, for CI and scripts. A new token is shown once."
      documentTitle="API tokens"
    />
  );
}

import { createFileRoute } from "@tanstack/react-router";

import { TokensSettings } from "../../../features/settings/TokensSettings";

/** Settings > API tokens: named, scoped tokens for CI and scripts. */
export const Route = createFileRoute("/_console/settings/tokens")({
  component: TokensSettings,
});

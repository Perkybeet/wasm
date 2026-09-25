import { createFileRoute } from "@tanstack/react-router";

import { GeneralSettings } from "../../../features/settings/GeneralSettings";

/** Settings > General: the typed sections of config.yaml, each saved on its own. */
export const Route = createFileRoute("/_console/settings/")({
  component: GeneralSettings,
});

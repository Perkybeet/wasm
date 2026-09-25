import { createFileRoute } from "@tanstack/react-router";

import { AboutSettings } from "../../../features/settings/AboutSettings";

/** Settings > About: version, update check, links and the terminal equivalents. */
export const Route = createFileRoute("/_console/settings/about")({
  component: AboutSettings,
});

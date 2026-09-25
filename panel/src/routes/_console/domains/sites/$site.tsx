import { createFileRoute } from "@tanstack/react-router";

import { SiteConfigPage } from "../../../../features/domains/SiteConfigPage";

/** One web server site and its configuration file. */
export const Route = createFileRoute("/_console/domains/sites/$site")({
  component: SiteRoute,
});

function SiteRoute() {
  const { site } = Route.useParams();
  // Keyed: another site is another editor, with none of this one's draft.
  return <SiteConfigPage key={site} site={site} />;
}

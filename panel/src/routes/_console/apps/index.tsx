import { createFileRoute } from "@tanstack/react-router";

import { AppsPage } from "../../../features/apps/AppsPage";
import { validateAppsSearch } from "../../../features/apps/filters";

/** Every application on the machine. Filters are search params: `/apps?state=failed`. */
export const Route = createFileRoute("/_console/apps/")({
  validateSearch: validateAppsSearch,
  component: ApplicationsRoute,
});

function ApplicationsRoute() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <AppsPage search={search} onSearchChange={(next, options) => void navigate({ search: next, replace: options?.replace ?? false })} />
  );
}

import { createFileRoute } from "@tanstack/react-router";

import { ActivityPage } from "../../features/activity/ActivityPage";
import { validateActivitySearch } from "../../features/activity/data";

/** The jobs timeline. Filters are search params: `/activity?status=failed&type=deploy`. */
export const Route = createFileRoute("/_console/activity")({
  validateSearch: validateActivitySearch,
  component: ActivityRoute,
});

function ActivityRoute() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <ActivityPage search={search} onSearchChange={(next, options) => void navigate({ search: next, replace: options?.replace ?? false })} />
  );
}

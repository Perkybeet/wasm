import { createFileRoute } from "@tanstack/react-router";

import { CronPage } from "../../features/cron/CronPage";
import { validateCronSearch } from "../../features/cron/data";

/** Every scheduled job on this machine. The search box is a search param: `/cron?q=backup`. */
export const Route = createFileRoute("/_console/cron")({
  validateSearch: validateCronSearch,
  component: CronRoute,
});

function CronRoute() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <CronPage search={search} onSearchChange={(next, options) => void navigate({ search: next, replace: options?.replace ?? false })} />
  );
}

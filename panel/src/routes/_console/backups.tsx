import { createFileRoute } from "@tanstack/react-router";

import { BackupsPage } from "../../features/backups/BackupsPage";
import { validateBackupsSearch } from "../../features/backups/filters";

/** Every backup on the machine. Filters are search params: `/backups?domain=shop.example.com`. */
export const Route = createFileRoute("/_console/backups")({
  validateSearch: validateBackupsSearch,
  component: BackupsRoute,
});

function BackupsRoute() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <BackupsPage search={search} onSearchChange={(next, options) => void navigate({ search: next, replace: options?.replace ?? false })} />
  );
}

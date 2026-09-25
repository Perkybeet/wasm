import { createFileRoute } from "@tanstack/react-router";

import { ServicesPage } from "../../../features/services/ServicesPage";
import { validateServicesSearch } from "../../../features/services/data";

/** Every systemd unit WASM manages. The search box is a search param: `/services?q=worker`. */
export const Route = createFileRoute("/_console/services/")({
  validateSearch: validateServicesSearch,
  component: ServicesRoute,
});

function ServicesRoute() {
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <ServicesPage search={search} onSearchChange={(next, options) => void navigate({ search: next, replace: options?.replace ?? false })} />
  );
}

import { createFileRoute } from "@tanstack/react-router";

import { DomainsPage, validateDomainsSearch } from "../../../features/domains/DomainsPage";

/** Certificates and sites, machine-wide. The open tab is a search param: `/domains?tab=sites`. */
export const Route = createFileRoute("/_console/domains/")({
  validateSearch: validateDomainsSearch,
  component: DomainsRoute,
});

function DomainsRoute() {
  const { tab = "certificates" } = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <DomainsPage
      tab={tab}
      onTabChange={(next) => void navigate({ search: next === "certificates" ? {} : { tab: next } })}
    />
  );
}

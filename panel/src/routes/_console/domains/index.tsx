import { createFileRoute } from "@tanstack/react-router";

import { DomainsPage } from "../../../features/domains/DomainsPage";
import { validateDomainsSearch } from "../../../features/domains/search";

/**
 * Certificates and sites, machine-wide. The open tab is a search param (`/domains?tab=sites`), and
 * so is the certificates' filter a link opens them with (`/domains?q=example.com`).
 */
export const Route = createFileRoute("/_console/domains/")({
  validateSearch: validateDomainsSearch,
  component: DomainsRoute,
});

function DomainsRoute() {
  const { tab = "certificates", q } = Route.useSearch();
  const navigate = Route.useNavigate();
  return (
    <DomainsPage
      tab={tab}
      {...(q !== undefined ? { filter: q } : {})}
      onTabChange={(next) => void navigate({ search: next === "certificates" ? {} : { tab: next } })}
    />
  );
}

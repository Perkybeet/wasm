/**
 * The Domains page's search params: the open tab, certificates unless the URL says sites. Kept
 * apart from `DomainsPage.tsx` so a route's `validateSearch` does not pull in that page's own
 * tabs (`CertificatesTab`, `SitesTab`) - importing anything from a module runs the whole of it,
 * heavy imports included, the way `features/overview/windows.ts` keeps `MachineCharts.tsx`'s
 * uPlot out of the overview route's own eager chunk.
 */

export type DomainsTab = "certificates" | "sites";

export interface DomainsSearch {
  tab?: DomainsTab;
}

/** The page's search params: the open tab, certificates unless the URL says sites. */
export function validateDomainsSearch(search: Record<string, unknown>): DomainsSearch {
  return search["tab"] === "sites" ? { tab: "sites" } : search["tab"] === "certificates" ? { tab: "certificates" } : {};
}

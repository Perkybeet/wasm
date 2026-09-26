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
  /**
   * What the certificates are filtered by when the page opens: a link from a health report or
   * an alert about one certificate lands on that certificate alone.
   */
  q?: string;
}

/** The page's search params: the open tab (certificates unless the URL says sites) and the certificates' filter. */
export function validateDomainsSearch(search: Record<string, unknown>): DomainsSearch {
  const tab = search["tab"];
  const q = search["q"];
  return {
    ...(tab === "sites" || tab === "certificates" ? { tab } : {}),
    ...(typeof q === "string" && q.trim() !== "" ? { q: q.trim() } : {}),
  };
}

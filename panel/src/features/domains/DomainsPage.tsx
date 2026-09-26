import { useQuery } from "@tanstack/react-query";

import { certsQuery } from "../../api/queries/certs";
import { sitesQuery } from "../../api/queries/sites";
import { PageHeader } from "../../app/PageHeader";
import { Tab, TabList, TabPanel, Tabs } from "../../components/ui/Tabs";
import { CertificatesTab } from "./CertificatesTab";
import type { DomainsTab } from "./search";
import { SitesTab } from "./SitesTab";

export interface DomainsPageProps {
  tab: DomainsTab;
  onTabChange: (tab: DomainsTab) => void;
}

/**
 * Certificates and web server sites, machine-wide, as two tabs whose choice is the URL.
 * An application's own names are managed from its Domains tab; this is every name on the box.
 */
export function DomainsPage({ tab, onTabChange }: DomainsPageProps) {
  const certs = useQuery(certsQuery());
  const sites = useQuery(sitesQuery());
  return (
    <>
      <PageHeader
        title="Domains and certificates"
        description="The TLS certificates on this machine and the web server sites that answer each name."
      />
      <Tabs<DomainsTab> value={tab} onValueChange={onTabChange}>
        <TabList aria-label="Domains and certificates">
          <Tab value="certificates" {...(certs.data ? { count: certs.data.total } : {})}>
            Certificates
          </Tab>
          <Tab value="sites" {...(sites.data ? { count: sites.data.total } : {})}>
            Sites
          </Tab>
        </TabList>
        {/* The tab names the panel for sight; the heading gives it a place in the outline. */}
        <TabPanel value="certificates">
          <h2 className="sr-only">Certificates</h2>
          <CertificatesTab />
        </TabPanel>
        <TabPanel value="sites">
          <h2 className="sr-only">Sites</h2>
          <SitesTab />
        </TabPanel>
      </Tabs>
    </>
  );
}

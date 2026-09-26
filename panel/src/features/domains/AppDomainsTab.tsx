import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowRight, Globe, MoreHorizontal, Plus, RotateCw, Search, Trash2 } from "lucide-react";
import { useState } from "react";

import { request } from "../../api/client";
import { certsQuery } from "../../api/queries/certs";
import { appDomainsQuery, dnsCheckQuery, domainKeys } from "../../api/queries/domains";
import type { AppDomain, AppDomainChange, AppDomainList } from "../../api/queries/domains";
import { activeJobsQuery, isJobFinished, useFollowedJob } from "../../api/queries/jobs";
import { useDocumentTitle } from "../../app/documentTitle";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section, Sections } from "../../components/page/Section";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import type { Column } from "../../components/ui/DataTable";
import { DataTable } from "../../components/ui/DataTable";
import { Dialog } from "../../components/ui/Dialog";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem, MenuSeparator } from "../../components/ui/Menu";
import { Skeleton } from "../../components/ui/Skeleton";
import { toast } from "../../components/ui/toast";
import { findCertificate } from "../app/lookups";
import { reportActionError } from "../apps/useAppActions";
import { AddDomainDialog } from "./AddDomainDialog";
import { CertificateStatus } from "./CertificateStatus";
import { DnsVerdict } from "./DnsVerdict";
import { certificateJobFor, certificateView, coverageOf, covers, issuerName } from "./certificates";
import { JobBanner } from "./JobBanner";
import { useCertificateRefresh } from "./useCertificateJobs";

const KIND_LABEL: Record<string, string> = { primary: "Primary", alias: "Alias", redirect: "Redirect" };

function Role({ entry, app }: { entry: AppDomain; app: string }) {
  if (entry.kind !== "redirect") return <Badge>{KIND_LABEL[entry.kind] ?? entry.kind}</Badge>;
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5">
      <Badge>Redirect</Badge>
      <ArrowRight aria-hidden="true" className="size-3.5 shrink-0 text-fg-faint" />
      <span className="sr-only">to </span>
      <span translate="no" className="truncate text-13 text-fg-muted">
        {app}
      </span>
    </span>
  );
}

function DnsDialog({ app, name, onClose }: { app: string; name: string | null; onClose: () => void }) {
  const dns = useQuery({ ...dnsCheckQuery(app, name ?? ""), enabled: name !== null });
  return (
    <Dialog
      open={name !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={name === null ? "DNS" : `Where ${name} points`}
      description="What the name resolves to right now, compared with this server's addresses."
      footer={
        <>
          <Button icon={<RotateCw aria-hidden="true" />} loading={dns.isFetching} onClick={() => void dns.refetch()}>
            Check again
          </Button>
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </>
      }
    >
      <div aria-live="polite">
        {dns.data ? (
          <DnsVerdict check={dns.data} />
        ) : dns.isError ? (
          <ErrorBlock compact error={dns.error} title={`Could not resolve ${name ?? ""}`} />
        ) : (
          <div aria-busy="true" className="flex flex-col gap-2">
            <span className="sr-only">Checking DNS</span>
            <Skeleton className="h-4 w-56" />
            <Skeleton className="h-12" />
          </div>
        )}
      </div>
    </Dialog>
  );
}

function TableSkeleton() {
  return (
    <div aria-hidden="true" className="flex flex-col divide-y divide-border rounded-card border border-border bg-surface">
      {[0, 1].map((i) => (
        <div key={i} className="flex h-11 items-center gap-6 px-4">
          <Skeleton className="h-3 w-40" />
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-3 w-24" />
        </div>
      ))}
    </div>
  );
}

/**
 * The names an application answers on: its primary domain, the aliases that serve it and the
 * redirects that send visitors to it, with what the certificate does for each. Adding a name
 * asks DNS first; removing one needs the operator to confirm it's them.
 */
export function AppDomainsTab({ domain }: { domain: string }) {
  useDocumentTitle(`Domains - ${domain}`);
  const queryClient = useQueryClient();
  const list = useQuery(appDomainsQuery(domain));
  const certs = useQuery(certsQuery());
  const active = useQuery(activeJobsQuery());
  useCertificateRefresh();
  const followed = useFollowedJob();

  const [adding, setAdding] = useState(false);
  const [lastAdded, setLastAdded] = useState<string | null>(null);
  const [dnsFor, setDnsFor] = useState<string | null>(null);
  const [removing, setRemoving] = useState<AppDomain | null>(null);

  const lineage = certs.isError && certs.data === undefined ? null : findCertificate(certs.data, domain);
  const running = certificateJobFor(active.data?.jobs, domain);
  const extending = running !== null || (followed.job !== null && !isJobFinished(followed.job)) || (followed.id !== null && followed.job === null);

  const applyChange = (change: AppDomainChange): void => {
    queryClient.setQueryData<AppDomainList>(domainKeys.list(domain), { app: change.app, domains: change.domains });
    void queryClient.invalidateQueries({ queryKey: domainKeys.list(domain) });
  };

  const onAdded = (change: AppDomainChange, name: string): void => {
    applyChange(change);
    setLastAdded(name);
    const kept = change.adopted ?? [];
    const adopted = kept.length > 0 ? ` Kept ${kept.join(", ")}, which the site already answered on, as ${kept.length === 1 ? "an alias" : "aliases"}.` : "";
    if (change.certificate_job_id) {
      followed.follow(change.certificate_job_id);
      toast.success(`Added ${name} to ${domain}`, { description: `Extending the certificate to cover it.${adopted}` });
    } else {
      const description = `${change.tls ? "" : "The site serves plain HTTP, so no certificate was ordered."}${adopted}`.trim();
      toast.success(`Added ${name} to ${domain}`, description === "" ? {} : { description });
    }
  };

  const retry = useMutation({
    mutationFn: (entry: AppDomain) =>
      request("post", "/api/apps/{domain}/domains", { params: { domain }, body: { domain: entry.domain, kind: entry.kind } }),
    onSuccess: (change, entry) => {
      applyChange(change);
      setLastAdded(entry.domain);
      if (change.certificate_job_id) followed.follow(change.certificate_job_id);
      else toast.info(`${domain} serves plain HTTP`, { description: "There is no certificate to extend." });
    },
    onError: (error, entry) => {
      reportActionError(`Could not retry the certificate for ${entry.domain}`, error);
    },
  });

  const columns: Column<AppDomain>[] = [
    {
      id: "domain",
      header: "Domain",
      cell: (entry) => (
        <a
          href={`${lineage && covers(lineage, entry.domain) ? "https" : "http"}://${entry.domain}`}
          target="_blank"
          rel="noreferrer"
          translate="no"
          className="-mx-1 rounded-[4px] px-1 py-0.5 font-medium text-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
        >
          {entry.domain}
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      ),
      sortValue: (entry) => entry.domain,
    },
    { id: "role", header: "Role", cell: (entry) => <Role entry={entry} app={domain} /> },
    {
      id: "certificate",
      header: "Certificate",
      cell: (entry) => {
        const coverage = coverageOf(entry.domain, lineage, extending);
        return <CertificateStatus tone={coverage.tone} label={coverage.label} />;
      },
    },
    {
      id: "added",
      header: "Added",
      hideBelow: "md",
      cell: (entry) => (entry.created_at ? <RelativeTime value={entry.created_at} className="text-fg-muted" /> : <span className="text-fg-faint">Unknown</span>),
    },
  ];

  return (
    <Sections>
      <Section
        title="Domains"
        description={`The names ${domain} answers on. Aliases serve the app; redirects send visitors to ${domain}.`}
        actions={
          <Button icon={<Plus aria-hidden="true" />} onClick={() => setAdding(true)}>
            Add domain
          </Button>
        }
      >
        <JobBanner
          followed={followed}
          words={{
            running: lastAdded === null ? `Extending the certificate of ${domain}` : `Extending the certificate to ${lastAdded}`,
            done: `The certificate covers every domain of ${domain}`,
            failed: "The certificate was not extended",
            hint: `${lastAdded ?? "The new name"} is served already, but browsers warn on HTTPS until the certificate covers it. Once DNS points here, retry it from its row.`,
          }}
        />
        {list.isError && list.data === undefined ? (
          <ErrorBlock error={list.error} title={`Could not load the domains of ${domain}`} onRetry={() => void list.refetch()} retrying={list.isRefetching} />
        ) : list.data === undefined ? (
          <div aria-busy="true">
            <span className="sr-only">Loading domains</span>
            <TableSkeleton />
          </div>
        ) : (
          <DataTable
            caption={`Domains of ${domain}`}
            columns={columns}
            rows={list.data.domains}
            getRowId={(entry) => entry.domain}
            empty={
              <EmptyState
                icon={<Globe />}
                title="No domains recorded"
                description="Add the names this app should answer on."
                className="border-0 py-8"
              />
            }
            rowActions={(entry) => (
              <Menu
                align="end"
                trigger={<IconButton label={`Actions for ${entry.domain}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}
              >
                <MenuItem icon={<Search />} onClick={() => setDnsFor(entry.domain)}>
                  Check DNS
                </MenuItem>
                {entry.kind !== "primary" && lineage && !covers(lineage, entry.domain) ? (
                  <MenuItem icon={<RotateCw />} disabled={retry.isPending || extending} onClick={() => retry.mutate(entry)}>
                    Retry certificate
                  </MenuItem>
                ) : null}
                {entry.kind !== "primary" ? (
                  <>
                    <MenuSeparator />
                    <MenuItem icon={<Trash2 />} destructive onClick={() => setRemoving(entry)}>
                      Remove
                    </MenuItem>
                  </>
                ) : null}
              </Menu>
            )}
          />
        )}
        <p className="max-w-[68ch] text-13 text-pretty text-fg-muted">
          {`${domain} is the primary domain: the app's unit, directory and site are named after it, so it cannot be removed here. To move the app to another name, deploy it there and delete this one.`}
        </p>
      </Section>

      <Section
        title="Certificate"
        description="One certificate covers every name of the app, redirects included."
        actions={
          <Link to="/domains" search={{ tab: "certificates" }} className="rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus">
            All certificates
          </Link>
        }
      >
        {lineage === undefined ? (
          <Skeleton className="h-16 rounded-card" />
        ) : lineage === null ? (
          <p className="text-13 text-fg-muted">
            {certs.isError ? "The certificates could not be listed." : `No certificate covers ${domain}: the site answers over plain HTTP.`}
          </p>
        ) : (
          <div className="flex flex-col gap-3 rounded-card border border-border bg-surface p-4 shadow-raised sm:flex-row sm:items-center sm:justify-between">
            <div className="flex min-w-0 flex-col gap-1">
              <CertificateStatus {...certificateView(lineage)} className="text-14" />
              <p className="text-13 text-fg-muted">
                {lineage.issuer ? `Issued by ${issuerName(lineage.issuer)}. ` : ""}
                {lineage.expires_on ? `Valid until ${lineage.expires_on}. ` : ""}
                {lineage.auto_renew ? "Renews on its own before it expires." : "Does not renew on its own."}
              </p>
            </div>
            <ul aria-label="Names on the certificate" className="flex min-w-0 flex-wrap gap-1.5 sm:justify-end">
              {lineage.domains.map((name) => (
                <li key={name}>
                  <Badge mono>{name}</Badge>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      <CommandHint command={`wasm domain list ${domain}`} label="From a terminal" />

      <AddDomainDialog app={domain} open={adding} onOpenChange={setAdding} onAdded={onAdded} />
      <DnsDialog app={domain} name={dnsFor} onClose={() => setDnsFor(null)} />
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => {
          if (!open) setRemoving(null);
        }}
        title={removing ? `Remove ${removing.domain}` : "Remove domain"}
        description={
          removing
            ? `${domain} stops answering on ${removing.domain} as soon as the site reloads. The certificate keeps covering the name until it is next renewed; nothing is revoked.`
            : ""
        }
        confirmText={removing?.domain ?? ""}
        actionLabel="Remove domain"
        onConfirm={async () => {
          if (!removing) return;
          const change = await request("delete", "/api/apps/{domain}/domains/{name}", {
            params: { domain, name: removing.domain },
          });
          applyChange(change);
          toast.success(`Removed ${removing.domain} from ${domain}`);
        }}
      />
    </Sections>
  );
}

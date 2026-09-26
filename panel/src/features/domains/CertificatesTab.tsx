import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Plus, RefreshCw, Search, ShieldCheck, ShieldX, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";

import { request } from "../../api/client";
import { certKeys, certsQuery } from "../../api/queries/certs";
import type { CertEntry } from "../../api/queries/certs";
import { activeJobsQuery, useFollowedJob } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";
import { CommandHint } from "../../components/page/CommandHint";
import { KeyValueList } from "../../components/page/KeyValueList";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import type { Column } from "../../components/ui/DataTable";
import { DataTable } from "../../components/ui/DataTable";
import { Drawer } from "../../components/ui/Drawer";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { Menu, MenuItem, MenuSeparator } from "../../components/ui/Menu";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";
import { CertificateStatus } from "./CertificateStatus";
import { IssueCertificateDialog } from "./IssueCertificateDialog";
import { JobBanner } from "./JobBanner";
import type { JobWords } from "./JobBanner";
import { byUrgency, certificateJobFor, certificateView, issuerName } from "./certificates";
import { truncatedNames } from "./names";
import { useCertificateRefresh } from "./useCertificateJobs";

/** The state a row shows: the job working on it, or what its expiry means. */
function rowState(cert: CertEntry, job: Job | null): { tone: "ok" | "warn" | "fail" | "idle" | "busy"; label: string } {
  if (job !== null) return { tone: "busy", label: job.type === "cert_renew" ? "Renewing" : "Issuing" };
  return certificateView(cert);
}

/** The names a certificate covers besides the one it is named after. */
function otherNames(cert: CertEntry): string[] {
  return cert.domains.filter((name) => name !== cert.domain);
}

function Names({ cert }: { cert: CertEntry }) {
  const others = otherNames(cert);
  if (others.length === 0) return <span className="text-fg-faint">Only this name</span>;
  const { shown, rest } = truncatedNames(others, 2);
  return (
    <span className="flex min-w-0 items-center gap-1.5" title={others.join(", ")}>
      <span translate="no" className="mono truncate text-12 text-fg-muted">
        {shown}
      </span>
      {rest > 0 ? <span className="shrink-0 text-12 text-fg-faint">{`+${String(rest)} more`}</span> : null}
    </span>
  );
}

function CertificateDrawer({
  cert,
  job,
  onClose,
  onRenew,
}: {
  cert: CertEntry | null;
  job: Job | null;
  onClose: () => void;
  onRenew: (cert: CertEntry) => void;
}) {
  return (
    <Drawer
      open={cert !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={cert?.domain ?? "Certificate"}
      description="As certbot reports it. The private key itself is never read."
      footer={
        cert ? (
          <Button icon={<RefreshCw aria-hidden="true" />} disabled={job !== null} onClick={() => onRenew(cert)}>
            Renew
          </Button>
        ) : undefined
      }
    >
      {cert ? (
        <div className="flex flex-col gap-5">
          <CertificateStatus {...rowState(cert, job)} className="text-14" />
          <KeyValueList
            items={[
              {
                label: "Names",
                value: cert.domains.join(", "),
                copy: cert.domains.join(" "),
                hint: `${String(cert.domains.length)} ${cert.domains.length === 1 ? "name" : "names"}`,
              },
              { label: "Issuer", value: cert.issuer ? issuerName(cert.issuer) : null, mono: false },
              { label: "Expires", value: cert.expires_on ?? null },
              { label: "Certbot says", value: cert.valid_until ?? null },
              { label: "Renewal", value: cert.auto_renew ? "Automatic" : "Manual", mono: false, copy: false },
              { label: "Certificate", value: cert.path ?? null },
              { label: "Private key", value: cert.key_path ?? null },
            ]}
          />
          <CommandHint command={`wasm cert info ${cert.domain}`} label="From a terminal" />
        </div>
      ) : null}
    </Drawer>
  );
}

/**
 * Every certificate certbot holds on this machine, the most urgent first: what it covers, when
 * it expires, and renewing, revoking and deleting it. Issuing and renewing run as jobs, followed
 * above the table.
 */
export function CertificatesTab() {
  const queryClient = useQueryClient();
  const certs = useQuery(certsQuery());
  const active = useQuery(activeJobsQuery());
  useCertificateRefresh();
  const followed = useFollowedJob();

  const [words, setWords] = useState<JobWords | null>(null);
  const [filter, setFilter] = useState("");
  const [issuing, setIssuing] = useState(false);
  const [opened, setOpened] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<CertEntry | null>(null);
  const [deleting, setDeleting] = useState<CertEntry | null>(null);

  const all = useMemo(() => [...(certs.data?.certificates ?? [])].sort(byUrgency), [certs.data]);
  const needle = filter.trim().toLowerCase();
  const shown = needle === "" ? all : all.filter((cert) => cert.domains.some((name) => name.includes(needle)) || cert.domain.includes(needle));
  const jobs = active.data?.jobs;
  const openedCert = all.find((cert) => cert.domain === opened) ?? null;

  const followJob = (jobId: string, next: JobWords): void => {
    setWords(next);
    followed.follow(jobId);
  };

  const renew = useMutation({
    mutationFn: ({ cert, force }: { cert: CertEntry; force: boolean }) =>
      request("post", "/api/certs/{domain}/renew", { params: { domain: cert.domain }, body: { force } }),
    onSuccess: (result, { cert, force }) => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      followJob(result.job_id, {
        running: `${force ? "Renewing" : "Renewing if due"} ${cert.domain}`,
        done: force ? `Renewed ${cert.domain}` : `Renewal of ${cert.domain} finished. Certbot renews only what is due; its log says which.`,
        failed: `Renewing ${cert.domain} failed`,
      });
    },
    onError: (error, { cert }) => {
      reportActionError(`Could not renew ${cert.domain}`, error);
    },
  });

  const renewAll = useMutation({
    mutationFn: () => request("post", "/api/certs/renew-all", { body: { force: false } }),
    onSuccess: (result) => {
      followJob(result.job_id, {
        running: "Renewing every certificate that is due",
        done: "Renewal finished. Certificates not yet due were left alone.",
        failed: "Renewing the due certificates failed",
      });
    },
    onError: (error) => {
      reportActionError("Could not start the renewal", error);
    },
  });

  const columns: Column<CertEntry>[] = [
    {
      id: "name",
      header: "Certificate",
      cell: (cert) => <span translate="no">{cert.domain}</span>,
      sortValue: (cert) => cert.domain,
    },
    { id: "names", header: "Also covers", hideBelow: "md", cell: (cert) => <Names cert={cert} /> },
    {
      id: "expires",
      header: "Expires",
      mono: true,
      hideBelow: "sm",
      cell: (cert) => cert.expires_on ?? <span className="text-fg-faint">Unknown</span>,
      sortValue: (cert) => cert.days_remaining ?? null,
    },
    {
      id: "state",
      header: "State",
      cell: (cert) => <CertificateStatus {...rowState(cert, certificateJobFor(jobs, cert.domain))} />,
      sortValue: (cert) => cert.days_remaining ?? null,
    },
    {
      id: "issuer",
      header: "Issuer",
      hideBelow: "lg",
      cell: (cert) => <span className="text-fg-muted">{cert.issuer ? issuerName(cert.issuer) : "Unknown"}</span>,
    },
    {
      id: "renewal",
      header: "Renewal",
      hideBelow: "lg",
      cell: (cert) => <span className="text-fg-muted">{cert.auto_renew ? "Automatic" : "Manual"}</span>,
    },
  ];

  const issueButton = (
    <Button variant="primary" icon={<Plus aria-hidden="true" />} onClick={() => setIssuing(true)}>
      Issue certificate
    </Button>
  );

  return (
    <div className="flex flex-col gap-4">
      {words !== null ? <JobBanner followed={followed} words={words} /> : null}

      {certs.isError && certs.data === undefined ? (
        <ErrorBlock
          error={certs.error}
          title="Could not list the certificates"
          hint="The list comes from certbot certificates; check that certbot is installed."
          onRetry={() => void certs.refetch()}
          retrying={certs.isRefetching}
        />
      ) : certs.data !== undefined && all.length === 0 ? (
        <EmptyState
          level={3}
          icon={<ShieldCheck />}
          title="No certificates yet"
          description="A certificate lets a site answer over HTTPS. Let's Encrypt issues one for any name that points to this server, and it renews itself."
          action={issueButton}
          command="wasm cert create -d example.com"
          className="py-16"
        />
      ) : (
        <>
          <div role="search" aria-label="Filter certificates" className="flex flex-wrap items-center gap-2">
            <Input
              type="search"
              aria-label="Filter certificates by name"
              placeholder="Filter by name"
              value={filter}
              onValueChange={(value: string) => setFilter(value)}
              icon={<Search />}
              className="w-full sm:w-64"
              autoComplete="off"
              spellCheck={false}
            />
            {filter !== "" ? (
              <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={() => setFilter("")}>
                Clear
              </Button>
            ) : null}
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <Button icon={<RefreshCw aria-hidden="true" />} loading={renewAll.isPending} onClick={() => renewAll.mutate()}>
                Renew due
              </Button>
              {issueButton}
            </div>
          </div>
          <DataTable
            caption={needle === "" ? "Certificates" : "Certificates matching the filter"}
            columns={columns}
            rows={shown}
            getRowId={(cert) => cert.domain}
            loading={certs.isPending}
            onRowActivate={(cert) => setOpened(cert.domain)}
            empty={
              <EmptyState
                title="No certificate matches"
                description={`None of the certificates covers a name containing "${filter.trim()}".`}
                className="border-0 py-8"
              />
            }
            rowActions={(cert) => {
              const busy = certificateJobFor(jobs, cert.domain) !== null;
              return (
                <Menu align="end" trigger={<IconButton label={`Actions for ${cert.domain}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
                  <MenuItem icon={<RefreshCw />} disabled={busy} onClick={() => renew.mutate({ cert, force: false })}>
                    Renew if due
                  </MenuItem>
                  <MenuItem icon={<RefreshCw />} disabled={busy} onClick={() => renew.mutate({ cert, force: true })}>
                    Renew now
                  </MenuItem>
                  <MenuSeparator />
                  <MenuItem icon={<ShieldX />} destructive onClick={() => setRevoking(cert)}>
                    Revoke
                  </MenuItem>
                  <MenuItem icon={<Trash2 />} destructive onClick={() => setDeleting(cert)}>
                    Delete
                  </MenuItem>
                </Menu>
              );
            }}
          />
          <CommandHint command="wasm cert list" label="From a terminal" />
        </>
      )}

      <IssueCertificateDialog
        open={issuing}
        onOpenChange={setIssuing}
        onQueued={(jobId, domain) =>
          followJob(jobId, {
            running: `Issuing a certificate for ${domain}`,
            done: `Issued a certificate for ${domain}`,
            failed: `Issuing a certificate for ${domain} failed`,
            hint: "Let's Encrypt could not verify every name. Check that each one points to this server, then issue again.",
          })
        }
      />
      <CertificateDrawer
        cert={openedCert}
        job={openedCert ? certificateJobFor(jobs, openedCert.domain) : null}
        onClose={() => setOpened(null)}
        onRenew={(cert) => renew.mutate({ cert, force: true })}
      />
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => {
          if (!open) setRevoking(null);
        }}
        title={revoking ? `Revoke ${revoking.domain}` : "Revoke certificate"}
        description="Let's Encrypt is told to stop trusting it, and its files are deleted. Every site that uses it stops serving HTTPS until a new certificate is issued. This cannot be undone."
        confirmText={revoking?.domain ?? ""}
        actionLabel="Revoke certificate"
        onConfirm={async () => {
          if (!revoking) return;
          await request("post", "/api/certs/{domain}/revoke", { params: { domain: revoking.domain } });
          void queryClient.invalidateQueries({ queryKey: certKeys.all });
          toast.success(`Revoked ${revoking.domain}`);
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => {
          if (!open) setDeleting(null);
        }}
        title={deleting ? `Delete ${deleting.domain}` : "Delete certificate"}
        description="Its files are deleted without revoking it, so a copy stays valid until it expires. Every site that uses it stops serving HTTPS until a new certificate is issued."
        confirmText={deleting?.domain ?? ""}
        actionLabel="Delete certificate"
        onConfirm={async () => {
          if (!deleting) return;
          await request("delete", "/api/certs/{domain}", { params: { domain: deleting.domain } });
          void queryClient.invalidateQueries({ queryKey: certKeys.all });
          toast.success(`Deleted ${deleting.domain}`);
        }}
      />
    </div>
  );
}

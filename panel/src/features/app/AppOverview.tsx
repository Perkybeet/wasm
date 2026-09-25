import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { CircleCheck, TriangleAlert, Webhook, X } from "lucide-react";
import type { ReactNode } from "react";

import { appQuery } from "../../api/queries/apps";
import type { App } from "../../api/queries/apps";
import { certsQuery } from "../../api/queries/certs";
import type { Cert } from "../../api/queries/certs";
import { deploymentsQuery } from "../../api/queries/deployments";
import { sitesQuery } from "../../api/queries/sites";
import { useDocumentTitle } from "../../app/documentTitle";
import { DeployStatePill } from "../../components/page/AppStatePill";
import { CommandHint } from "../../components/page/CommandHint";
import { KeyValueList, KeyValueListSkeleton } from "../../components/page/KeyValueList";
import type { KeyValueItem } from "../../components/page/KeyValueList";
import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { ResourceMeter } from "../../components/page/ResourceMeter";
import { Section } from "../../components/page/Section";
import { StatTile } from "../../components/page/StatTile";
import { useNow } from "../../components/page/clock";
import { appStatus, deployStatus } from "../../components/page/status";
import { Skeleton } from "../../components/ui/Skeleton";
import { STATUS, StatusGlyph } from "../../components/ui/StatusPill";
import { Tooltip } from "../../components/ui/Tooltip";
import { cx } from "../../lib/cx";
import { formatBytes, formatCount, formatDateTime, formatDuration, formatPercent, parseTimestamp } from "../../lib/format";
import type { Deployment } from "../apps/data";
import { appLimits, appReading, deployMoment, useLatestMetrics } from "../apps/data";
import { CERT_WARNING_DAYS } from "../overview/attention";
import { findCertificate, findSite, releasesQuery, webhookDeliveriesQuery } from "./queries";

const TONE_TEXT = { ok: "text-ok", warn: "text-warn", fail: "text-fail", idle: "text-idle" } as const;
const LINK =
  "rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus";

/** How many deploys the overview draws as dots; the rest are on the Deployments tab. */
const DOTS = 5;

/** A surface for a section's content. */
function Panel({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("min-w-0 rounded-card border border-border bg-surface px-4 py-2 shadow-raised", className)}>{children}</div>;
}

// ---------------------------------------------------------------------------------------
// The tiles

/** The last deploys as state dots, oldest first, each opening its deployment. */
function DeployDots({ domain, deploys }: { domain: string; deploys: readonly Deployment[] }) {
  const ordered = [...deploys].reverse();
  return (
    <ol aria-label={`Last ${String(ordered.length)} deploys, oldest first`} className="-ml-1 flex items-center gap-0.5">
      {ordered.map((deploy) => {
        const view = deployStatus(deploy.status);
        const moment = parseTimestamp(deployMoment(deploy));
        const when = moment === null ? "" : `, ${formatDateTime(moment)}`;
        const commit = deploy.git_commit ? ` ${deploy.git_commit.slice(0, 7)}` : "";
        return (
          <li key={deploy.id}>
            <Tooltip content={`${view.label}${commit}${when}`}>
              <Link
                to="/apps/$domain/deployments/$id"
                params={{ domain, id: String(deploy.id) }}
                aria-label={`Deploy ${String(deploy.id)}: ${view.label}${commit}${when}`}
                className={cx(
                  "flex size-7 items-center justify-center rounded-pill hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus",
                  TONE_TEXT[STATUS[view.state].tone],
                )}
              >
                <StatusGlyph state={view.state} size={14} />
              </Link>
            </Tooltip>
          </li>
        );
      })}
    </ol>
  );
}

/** How long the unit has been up, kept current. */
function UpFor({ since }: { since: Date }) {
  const now = useNow(() => 60_000);
  return <>{formatDuration(Math.max(0, (now - since.getTime()) / 1000))}</>;
}

function UptimeTile({ app }: { app: App }) {
  const state = appStatus(app.status).state;
  if (state === "static") return <StatTile label="Uptime" value="Always on" detail="Served by the web server, no process" />;
  if (!app.active) return <StatTile label="Uptime" value="Not running" detail={app.enabled ? "Starts at boot" : "Does not start at boot"} />;
  const since = parseTimestamp(app.uptime);
  if (since === null) return <StatTile label="Uptime" value="Running" detail={app.uptime ?? "Start time not reported"} />;
  return (
    <StatTile
      label="Uptime"
      value={
        <span className="title text-18 text-fg">
          <UpFor since={since} />
        </span>
      }
      detail={
        <>
          {"Started "}
          <RelativeTime value={since} />
        </>
      }
    />
  );
}

function CertificateTile({ cert, error }: { cert: Cert | null | undefined; error: unknown }) {
  if (cert === undefined && error) return <StatTile label="Certificate" value="Unknown" detail="The certificates could not be listed" />;
  if (cert === undefined) return <StatTile label="Certificate" value={<Skeleton className="h-5 w-24" />} />;
  if (cert === null) return <StatTile label="Certificate" value="None" detail="No certificate covers this domain" />;
  const days = cert.days_remaining;
  if (days === null || days === undefined) return <StatTile label="Certificate" value="Issued" detail={cert.valid_until ?? undefined} />;
  const icon =
    days < 0 ? (
      <X aria-hidden="true" className="size-4 text-fail" />
    ) : days < CERT_WARNING_DAYS ? (
      <TriangleAlert aria-hidden="true" className="size-4 text-warn" />
    ) : (
      <CircleCheck aria-hidden="true" className="size-4 text-ok" />
    );
  const text = days < 0 ? "Expired" : `${formatCount(days)} ${days === 1 ? "day" : "days"} left`;
  return (
    <StatTile
      label="Certificate"
      value={
        <span className="flex items-center gap-2">
          {icon}
          <span className="title text-18 text-fg">{text}</span>
        </span>
      }
      detail={cert.expires_on ? `Valid until ${cert.expires_on}` : undefined}
    />
  );
}

function Tiles({ app }: { app: App }) {
  const domain = app.domain;
  const deploys = useQuery(deploymentsQuery({ domain, limit: DOTS }));
  const releases = useQuery({ ...releasesQuery(domain), enabled: app.layout === "releases" });
  const certs = useQuery(certsQuery());

  const items = deploys.data?.items ?? [];
  const newest = items[0];
  const lastGood = items.find((deploy) => deploy.status === "success");
  const active = releases.data?.items.find((release) => release.active);

  let current: ReactNode;
  if (active) {
    current = (
      <StatTile
        label="Current release"
        value={active.commit?.slice(0, 7) ?? active.id}
        mono
        detail={
          <>
            {`Release ${active.id}, `}
            <RelativeTime value={active.activated_at ?? active.created_at} />
          </>
        }
      />
    );
  } else if (deploys.isPending) {
    current = <StatTile label="Current release" value={<Skeleton className="h-5 w-20" />} />;
  } else if (lastGood) {
    current = (
      <StatTile
        // In place, the checkout is not reported; what is known is the last deploy that worked.
        label={app.layout === "releases" ? "Current release" : "Last good deploy"}
        value={lastGood.git_commit?.slice(0, 7) ?? `Deploy ${String(lastGood.id)}`}
        mono
        detail={
          <>
            {lastGood.git_branch ? `${lastGood.git_branch}, deployed ` : "Deployed "}
            <RelativeTime value={deployMoment(lastGood)} />
          </>
        }
      />
    );
  } else {
    current = <StatTile label="Last good deploy" value="None recorded" detail="No deploy of this app has succeeded yet" />;
  }

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {current}
      <StatTile
        label="Recent deploys"
        value={
          deploys.isPending ? (
            <Skeleton className="h-5 w-28" />
          ) : items.length > 0 ? (
            <DeployDots domain={domain} deploys={items} />
          ) : (
            <span className="title text-18 text-fg">None yet</span>
          )
        }
        detail={
          newest ? (
            <>
              {`Last ${deployStatus(newest.status).label.toLowerCase()} `}
              <RelativeTime value={deployMoment(newest)} />
            </>
          ) : deploys.isPending ? undefined : (
            "Deploys and updates appear here"
          )
        }
      />
      <UptimeTile app={app} />
      <CertificateTile cert={findCertificate(certs.data, domain)} error={certs.error} />
    </div>
  );
}

/**
 * The newest deploy failed: said at the top, with the error's first line verbatim and the two
 * places to go next. Deliberately not live: it describes the past, and the failure was
 * announced when it happened.
 */
function LastDeployFailed({ domain }: { domain: string }) {
  const deploys = useQuery(deploymentsQuery({ domain, limit: DOTS }));
  const newest = deploys.data?.items[0];
  if (newest?.status !== "failed") return null;
  const line = newest.error
    ?.split("\n")
    .map((part) => part.trim())
    .find((part) => part !== "");
  return (
    <div className="flex flex-col gap-2 rounded-card border border-fail/30 bg-fail-soft/50 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="flex min-w-0 items-start gap-2.5">
        <StatusGlyph state="failed" size={14} className="mt-0.5 text-fail" />
        <div className="flex min-w-0 flex-col gap-0.5">
          <p className="text-13 font-medium text-fg">
            {"The last deploy failed "}
            <RelativeTime value={deployMoment(newest)} className="font-normal text-fg-muted" />
          </p>
          {line !== undefined ? (
            <code translate="no" title={line} className="truncate text-12 text-fg-muted">
              {line}
            </code>
          ) : null}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-4 pl-6 sm:pl-0">
        <Link to="/apps/$domain/deployments/$id" params={{ domain, id: String(newest.id) }} className={LINK}>
          View log
        </Link>
        <Link to="/apps/$domain/diagnose" params={{ domain }} className={LINK}>
          Diagnose
        </Link>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------
// The sections

function Domains({ app }: { app: App }) {
  const domain = app.domain;
  const certs = useQuery(certsQuery());
  const sites = useQuery(sitesQuery());
  const cert = findCertificate(certs.data, domain);
  const site = findSite(sites.data, domain);

  const names = cert?.domains.length ? cert.domains : [domain];
  const days = cert?.days_remaining ?? null;
  const certState: { tone: keyof typeof TONE_TEXT; text: string } =
    cert === null
      ? { tone: "idle", text: "No certificate" }
      : days === null
        ? { tone: "ok", text: "Certificate issued" }
        : days < 0
          ? { tone: "fail", text: "Certificate expired" }
          : days < CERT_WARNING_DAYS
            ? { tone: "warn", text: `Certificate expires in ${String(days)} days` }
            : { tone: "ok", text: `Certificate valid for ${String(days)} days` };
  const certMissing = cert === null;

  return (
    <Section
      title="Domains"
      actions={
        <Link to="/apps/$domain/domains" params={{ domain }} className={LINK}>
          Manage domains
        </Link>
      }
    >
      <Panel className="py-1">
        {cert === undefined && certs.isPending ? (
          <KeyValueListSkeleton rows={2} />
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {names.map((name) => (
              <li key={name} className="flex min-h-10 flex-wrap items-center justify-between gap-x-4 gap-y-1 py-2">
                <a
                  href={`${certMissing ? "http" : "https"}://${name}`}
                  target="_blank"
                  rel="noreferrer"
                  translate="no"
                  className="rounded-[4px] text-13 font-medium text-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
                >
                  {name}
                  <span className="sr-only"> (opens in a new tab)</span>
                </a>
                <span className={cx("flex items-center gap-1.5 text-12", TONE_TEXT[certState.tone])}>
                  <StatusGlyph state={certState.tone === "ok" ? "running" : certState.tone === "fail" ? "failed" : "stopped"} size={10} />
                  <span className={certState.tone === "idle" ? "text-fg-muted" : "text-fg"}>{certState.text}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
      <p className="text-12 text-fg-muted">
        {site
          ? `${site.webserver} site ${site.enabled ? "enabled" : "disabled"}${site.has_ssl ? ", configured for HTTPS" : ", HTTP only"}. `
          : site === null
            ? "No web server site is configured for this domain. "
            : ""}
        {cert?.auto_renew ? "The certificate renews automatically." : ""}
      </p>
    </Section>
  );
}

function WebhookSection({ domain }: { domain: string }) {
  const deliveries = useQuery(webhookDeliveriesQuery(domain));
  const latest = deliveries.data?.items[0];
  const total = deliveries.data?.total ?? 0;
  return (
    <Section
      title="Webhook"
      description="Deploys started by a push to the repository."
      actions={
        <Link to="/apps/$domain/settings" params={{ domain }} className={LINK}>
          Webhook settings
        </Link>
      }
    >
      {deliveries.isError && deliveries.data === undefined ? (
        <ErrorBlock compact error={deliveries.error} title="Could not load webhook deliveries" onRetry={() => void deliveries.refetch()} />
      ) : (
        <Panel className="flex min-h-12 items-center gap-3 py-3">
          <Webhook aria-hidden="true" className="size-4 shrink-0 text-fg-faint" />
          {deliveries.isPending ? (
            <Skeleton className="h-3.5 w-56" />
          ) : latest ? (
            <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-13">
              <DeployStatePill status={latest.status} appearance="inline" size="sm" />
              {latest.git_commit ? (
                <span translate="no" className="mono text-12 text-fg">
                  {latest.git_commit.slice(0, 7)}
                </span>
              ) : null}
              <RelativeTime value={latest.started_at} className="text-fg-muted" />
              <span className="text-fg-faint">{`${formatCount(total)} ${total === 1 ? "delivery" : "deliveries"} in total`}</span>
            </div>
          ) : (
            <p className="text-13 text-fg-muted">No push has deployed this app yet.</p>
          )}
        </Panel>
      )}
    </Section>
  );
}

function Runtime({ app }: { app: App }) {
  const staticSite = appStatus(app.status).state === "static";
  const items: KeyValueItem[] = [
    ...(staticSite ? [] : [{ label: "Port", value: app.port ?? null }]),
    ...(app.active && app.pid ? [{ label: "Main PID", value: app.pid }] : []),
    ...(staticSite ? [] : [{ label: "Starts at boot", value: app.enabled ? "Yes" : "No", mono: false, copy: false as const }]),
    {
      label: "Layout",
      value: app.layout === "releases" ? "Releases" : "In place",
      mono: false,
      copy: false,
      hint: app.layout === "releases" ? "Each deploy is a release; rollback is instant" : "Updated in its directory",
    },
    { label: "Directory", value: app.path ?? null },
  ];
  return (
    <Section title="Runtime">
      <Panel className="py-1">
        <KeyValueList empty="None" items={items} />
      </Panel>
      <CommandHint command={`wasm status ${app.domain}`} label="From a terminal" />
    </Section>
  );
}

function Resources({ app }: { app: App }) {
  const metrics = useLatestMetrics();
  const reading = appReading(metrics, app.domain);
  const limits = appLimits(app);
  const missing = !app.active ? "Not running" : metrics === undefined ? "Waiting" : "No reading";
  const nothing = reading.cpu === null && reading.memory === null && limits.cpu === null && limits.memory === null && limits.tasks === null;

  let body: ReactNode;
  if (nothing) {
    body = (
      <p className="text-13 text-pretty text-fg-muted">
        {app.active
          ? "No reading yet. The panel samples the CPU and memory of each app's unit every few seconds while it runs."
          : "Not running, so there is nothing to measure."}{" "}
        No limits are set for its unit.
      </p>
    );
  } else {
    body = (
      <>
        <ResourceMeter label="CPU" value={reading.cpu} limit={limits.cpu} format={formatPercent} missing={missing} />
        <ResourceMeter label="Memory" value={reading.memory} limit={limits.memory} format={formatBytes} missing={missing} />
        {limits.tasks !== null ? (
          <div className="flex items-baseline justify-between gap-3 text-13">
            <span className="text-fg-muted">Tasks</span>
            <span className="text-fg-faint">{`Limit ${formatCount(limits.tasks)}`}</span>
          </div>
        ) : null}
      </>
    );
  }

  return (
    <Section title="Resources" description={nothing ? undefined : "Use now against the unit's limits."}>
      <Panel className="flex flex-col gap-4 py-4">{body}</Panel>
    </Section>
  );
}

function OverviewSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-8">
      <span className="sr-only">Loading the application</span>
      <div aria-hidden="true" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="flex flex-col gap-2.5 rounded-card border border-border bg-surface px-4 py-3.5">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-5 w-28" />
            <Skeleton className="h-3 w-24" />
          </div>
        ))}
      </div>
      <div aria-hidden="true" className="grid gap-8 lg:grid-cols-2">
        <div className="rounded-card border border-border bg-surface px-4 py-1">
          <KeyValueListSkeleton rows={3} />
        </div>
        <div className="rounded-card border border-border bg-surface px-4 py-1">
          <KeyValueListSkeleton rows={6} />
        </div>
      </div>
    </div>
  );
}

/**
 * An app at a glance: what it runs, how its deploys went, how long it has been up, its
 * certificate; then its domains, webhook, runtime facts and resources against its limits.
 */
export function AppOverview({ domain }: { domain: string }) {
  useDocumentTitle(`Overview - ${domain}`, 1);
  const app = useQuery(appQuery(domain));

  // The layout owns the load failure and the not-found page; this shows the shape meanwhile.
  if (app.data === undefined) return app.isError ? null : <OverviewSkeleton />;

  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-3">
        <LastDeployFailed domain={domain} />
        <Tiles app={app.data} />
      </div>
      <div className="grid gap-8 lg:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-8">
          <Domains app={app.data} />
          <WebhookSection domain={domain} />
        </div>
        <div className="flex min-w-0 flex-col gap-8">
          <Runtime app={app.data} />
          {/* A static site has no process, so nothing to measure or limit. */}
          {appStatus(app.data.status).state === "static" ? null : <Resources app={app.data} />}
        </div>
      </div>
    </div>
  );
}

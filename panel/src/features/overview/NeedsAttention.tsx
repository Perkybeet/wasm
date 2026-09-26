import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { appsQuery } from "../../api/queries/apps";
import { certsQuery } from "../../api/queries/certs";
import { observationsQuery } from "../../api/queries/monitor";
import { servicesQuery } from "../../api/queries/services";
import { machineQuery } from "../../api/queries/system";
import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section } from "../../components/page/Section";
import { useAnnounceChange } from "../../components/page/useAnnounceChange";
import { Badge } from "../../components/ui/Badge";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusGlyph } from "../../components/ui/StatusPill";
import { cx } from "../../lib/cx";
import { describeError } from "../../lib/errors";
import { recentDeploysQuery } from "../apps/data";
import { collectAttention } from "./attention";
import type { AttentionItem, Severity } from "./attention";

const LINK =
  "rounded-[4px] font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus";

function SeverityGlyph({ severity }: { severity: Severity }) {
  return <StatusGlyph state={severity === "fail" ? "failed" : "warning"} size={14} className={severity === "fail" ? "text-fail" : "text-warn"} />;
}

/** The page the item's title opens: the app, or the page that owns what it is about. */
function SubjectLink({ item }: { item: AttentionItem }) {
  const className =
    "min-w-0 truncate rounded-[4px] text-14 font-medium text-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus";
  const label = <span translate="no">{item.title}</span>;
  switch (item.subject.kind) {
    case "app":
      return (
        <Link to="/apps/$domain" params={{ domain: item.subject.domain }} className={className}>
          {label}
        </Link>
      );
    case "certificate":
      return (
        <Link to="/domains" className={className}>
          {label}
        </Link>
      );
    case "units":
      return (
        <Link to="/services" className={className}>
          {item.title}
        </Link>
      );
    case "unit":
      return (
        <Link to="/services/$name" params={{ name: item.subject.name }} className={cx(className, "mono text-13")}>
          {label}
        </Link>
      );
    case "monitor":
      return (
        <Link to="/server" className={className}>
          {label}
          <span className="mono ml-1.5 text-12 font-normal text-fg-faint">{`PID ${String(item.subject.pid)}`}</span>
        </Link>
      );
  }
}

/** Where each problem of an app is dealt with: its deploy's log, its diagnosis, its domains. */
function ItemActions({ item }: { item: AttentionItem }) {
  if (item.subject.kind !== "app") return null;
  const domain = item.subject.domain;
  const kinds = new Set(item.reasons.map((reason) => reason.kind));
  const deploymentId = item.reasons.find((reason) => reason.deploymentId !== undefined)?.deploymentId;
  return (
    <div className="flex shrink-0 items-center gap-3 text-13">
      {deploymentId !== undefined ? (
        <Link
          to="/apps/$domain/deployments/$id"
          params={{ domain, id: String(deploymentId) }}
          aria-label={`View log of the deploy of ${domain}`}
          className={LINK}
        >
          View log
        </Link>
      ) : null}
      {kinds.has("state") || kinds.has("deploy") ? (
        <Link to="/apps/$domain/diagnose" params={{ domain }} aria-label={`Diagnose ${domain}`} className={LINK}>
          Diagnose
        </Link>
      ) : null}
      {kinds.has("certificate") ? (
        <Link to="/apps/$domain/domains" params={{ domain }} aria-label={`Certificate of ${domain}`} className={LINK}>
          Certificate
        </Link>
      ) : null}
    </div>
  );
}

function Item({ item }: { item: AttentionItem }) {
  const when = item.reasons.find((reason) => reason.when)?.when ?? null;
  return (
    <li
      data-severity={item.severity}
      className="grid grid-cols-[1rem_minmax(0,1fr)] gap-x-3 gap-y-1 px-4 py-3 sm:grid-cols-[1rem_minmax(0,1fr)_auto]"
    >
      <span className="flex h-5 items-center">
        <SeverityGlyph severity={item.severity} />
        <span className="sr-only">{item.severity === "fail" ? "Failure: " : "Warning: "}</span>
      </span>
      <div className="flex min-w-0 flex-col gap-1">
        <div className="flex min-w-0 items-baseline gap-2">
          <SubjectLink item={item} />
          {when ? <RelativeTime value={when} className="shrink-0 text-12 text-fg-faint" /> : null}
        </div>
        <ul className="flex min-w-0 flex-col gap-1">
          {item.reasons.map((reason, index) => (
            <li key={`${reason.summary}-${String(index)}`} className="flex min-w-0 flex-col">
              <span className={cx("text-13", reason.severity === "fail" ? "text-fg" : "text-fg-muted")}>{reason.summary}</span>
              {reason.detail !== undefined ? (
                <code translate="no" title={reason.detail} className="truncate text-12 text-fg-muted">
                  {reason.detail}
                </code>
              ) : null}
            </li>
          ))}
        </ul>
      </div>
      <div className="col-start-2 sm:col-start-3 sm:row-start-1 sm:self-center">
        <ItemActions item={item} />
      </div>
    </li>
  );
}

/** Where the block's last rendered height is kept, so the next load reserves as much. */
export const ATTENTION_HEIGHT_KEY = "wasm.overview.attention-height";
/** One skeleton row with its divider. */
const SKELETON_ROW = 63;

/** How tall the block was when the overview last showed it, in CSS pixels; null if unknown. */
export function rememberedAttentionHeight(): number | null {
  try {
    const value = Number(window.localStorage.getItem(ATTENTION_HEIGHT_KEY));
    return Number.isFinite(value) && value > 0 ? value : null;
  } catch {
    // Storage can be disabled (privacy modes): the skeleton falls back to its own two rows.
    return null;
  }
}

function rememberAttentionHeight(height: number): void {
  try {
    window.localStorage.setItem(ATTENTION_HEIGHT_KEY, String(Math.round(height)));
  } catch {
    // Not kept: the next load reserves the default instead, and may shift once.
  }
}

function SkeletonRows({ count }: { count: number }) {
  return (
    <div aria-hidden="true" className="divide-y divide-border rounded-card border border-border bg-surface shadow-raised">
      {Array.from({ length: count }, (_, i) => i).map((i) => (
        <div key={i} className="grid grid-cols-[1rem_minmax(0,1fr)] gap-3 px-4 py-3.5">
          <Skeleton className="mt-0.5 size-3.5 rounded-pill" />
          <div className="flex flex-col gap-2">
            <Skeleton className="h-3.5 w-48" />
            <Skeleton className="h-3 w-72 max-w-full" />
          </div>
        </div>
      ))}
    </div>
  );
}

/** A source that could not be checked: said plainly, with its own words, never silently skipped. */
function Unchecked({ what, error }: { what: string; error: unknown }) {
  const { detail } = describeError(error);
  return (
    <p className="flex min-w-0 items-baseline gap-2 text-12 text-fg-muted">
      <TriangleAlert aria-hidden="true" className="size-3 shrink-0 translate-y-0.5 text-warn" />
      <span className="min-w-0">
        {`${what} could not be checked: `}
        <code className="break-words text-fg">{detail}</code>
      </span>
    </p>
  );
}

/**
 * The top of the overview: every problem on the machine, worst first, each linking to where
 * it is fixed. When there is none, one quiet line says what was checked.
 */
export interface NeedsAttentionProps {
  /** Called once every source has answered and the block has its final height. */
  onSettled?: () => void;
}

export function NeedsAttention({ onSettled }: NeedsAttentionProps = {}) {
  const apps = useQuery(appsQuery());
  const deploys = useQuery(recentDeploysQuery());
  const certs = useQuery(certsQuery());
  const observations = useQuery(observationsQuery(false));
  const machine = useQuery(machineQuery());
  const units = useQuery(servicesQuery());

  const items = useMemo(
    () =>
      collectAttention({
        apps: apps.data?.apps,
        deployments: deploys.data?.items,
        certificates: certs.data?.certificates,
        observations: observations.data?.observations,
        machine: machine.data,
        units: units.data?.services,
      }),
    [apps.data, deploys.data, certs.data, observations.data, machine.data, units.data],
  );

  // Every source answers (or fails) before any item is drawn: items arriving one source at a
  // time would grow the block, and push the charts and applications below it, once per source.
  const loading = [apps, deploys, certs, observations, machine, units].some((query) => query.isPending);
  const count = loading ? null : items.length;

  // The space the block took last time is held for it while it loads, so on the usual reload
  // (the same problems as a minute ago) nothing below it moves when it lands.
  const [reserved] = useState(rememberedAttentionHeight);
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!loading) onSettled?.();
  }, [loading, onSettled]);
  useEffect(() => {
    const body = bodyRef.current;
    if (loading || !body) return;
    const observer = new ResizeObserver(() => {
      rememberAttentionHeight(body.offsetHeight);
    });
    observer.observe(body);
    rememberAttentionHeight(body.offsetHeight);
    return () => {
      observer.disconnect();
    };
  }, [loading]);
  useAnnounceChange(
    count === null ? null : String(count),
    count === null ? null : count === 0 ? "Nothing needs attention" : `${String(count)} ${count === 1 ? "item needs" : "items need"} attention`,
    items.some((item) => item.severity === "fail") ? "assertive" : "polite",
  );

  const unchecked: { what: string; error: unknown }[] = [
    ...(certs.isError ? [{ what: "Certificates", error: certs.error }] : []),
    ...(observations.isError ? [{ what: "Monitor findings", error: observations.error }] : []),
  ];

  let body: ReactNode;
  if (apps.isError && apps.data === undefined) {
    body = <ErrorBlock error={apps.error} title="Could not check the applications" onRetry={() => void apps.refetch()} retrying={apps.isRefetching} />;
  } else if (loading) {
    body = (
      <div aria-busy="true">
        <span className="sr-only">Checking the machine</span>
        <SkeletonRows count={reserved === null ? 2 : Math.max(1, Math.round(reserved / SKELETON_ROW))} />
      </div>
    );
  } else if (items.length === 0) {
    body = (
      <div className="flex items-start gap-3 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
        <span className="flex h-5 items-center">
          <StatusGlyph state="running" size={14} className="text-ok" />
        </span>
        <p className="text-13 text-fg-muted">
          <span className="font-medium text-fg">Nothing needs attention.</span> No failed deploys or units, no certificate
          expiring within three weeks, no open monitor findings.
        </p>
      </div>
    );
  } else {
    body = (
      <ul className="divide-y divide-border rounded-card border border-border bg-surface shadow-raised">
        {items.map((item) => (
          <Item key={item.id} item={item} />
        ))}
      </ul>
    );
  }

  const worst = items[0]?.severity;
  return (
    <Section
      title="Needs attention"
      badge={
        count !== null && count > 0 ? (
          <Badge tone={worst === "fail" ? "fail" : "warn"}>
            <span className="sr-only">Items: </span>
            {count}
          </Badge>
        ) : undefined
      }
    >
      <div ref={bodyRef} style={loading && reserved !== null ? { minHeight: reserved } : undefined}>
        {body}
      </div>
      {unchecked.length > 0 ? (
        <div className="-mt-1 flex flex-col gap-1">
          {unchecked.map((source) => (
            <Fragment key={source.what}>
              <Unchecked what={source.what} error={source.error} />
            </Fragment>
          ))}
        </div>
      ) : null}
    </Section>
  );
}

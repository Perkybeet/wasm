import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChevronLeft, FileX } from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";

import { isApiError } from "../../../api/client";
import { appQuery } from "../../../api/queries/apps";
import { deploymentLogQuery, deploymentQuery } from "../../../api/queries/deployments";
import type { Deployment } from "../../../api/queries/deployments";
import { useDocumentTitle } from "../../../app/documentTitle";
import { DeployStatePill } from "../../../components/page/AppStatePill";
import { useNow } from "../../../components/page/clock";
import { ErrorBlock } from "../../../components/page/QueryState";
import { RelativeTime } from "../../../components/page/RelativeTime";
import { Section } from "../../../components/page/Section";
import { useAnnounceChange } from "../../../components/page/useAnnounceChange";
import { Button } from "../../../components/ui/Button";
import { CopyButton } from "../../../components/ui/CopyButton";
import { EmptyState } from "../../../components/ui/EmptyState";
import { LogViewer } from "../../../components/ui/LogViewer";
import type { LogLine } from "../../../components/ui/LogViewer";
import { Skeleton } from "../../../components/ui/Skeleton";
import { StatusPill } from "../../../components/ui/StatusPill";
import { formatBytes, formatDuration, parseTimestamp } from "../../../lib/format";
import { hasUnit } from "../../apps/AppRowActions";
import { buildLogEvents, jobEvents, logClockText, logSpan, mergeEvents, useLogLines } from "./buildLog";
import { DeploymentActions } from "./DeploymentActions";
import { PhaseTimeline } from "./PhaseTimeline";
import { currentPhase, logClockOffset, outcomeOf, timeline } from "./phases";
import type { PhaseKey, PhaseView } from "./phases";
import { useDeploymentJob } from "./useDeploymentJob";
import { shortCommit, triggerWords } from "./words";

const RUNNING = new Set(["queued", "running"]);

/** Bytes of the captured log read by default: the backend's own default tail. */
const DEFAULT_TAIL = 512 * 1024;
/** Bytes asked for when the operator wants the whole log. */
const WHOLE_LOG = 64 * 1024 * 1024;

const LINK =
  "rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus";

/** What went wrong, by the phase it stopped in, above the system's own words. */
const FAILURE: Readonly<Record<PhaseKey, { title: string; hint: string }>> = {
  fetch: {
    title: "The deploy failed while fetching the source",
    hint: "Check that the repository, the branch and the deploy key still work from this machine. The git output is in the log below.",
  },
  install: {
    title: "The deploy failed while installing dependencies",
    hint: "The package manager's output is in the log below. A lockfile out of step with package.json is the usual cause.",
  },
  build: {
    title: "The deploy failed while building",
    hint: "The build's own output is in the log below. Fix the error, push, and deploy again. The version serving now was not touched.",
  },
  activate: {
    title: "The deploy failed while starting the new version",
    hint: "The unit's journal says why it did not start. Diagnose reads it together with the port and the web server.",
  },
  health: {
    title: "The new version did not pass its health check",
    hint: "It never answered on its port, so the version that was serving before was put back. The journal in the error below says why.",
  },
};

function failureWords(phase: PhaseView | null): { title: string; hint: string } {
  if (phase === null) return { title: "The deploy failed", hint: "The log below shows how far it got." };
  return FAILURE[phase.key];
}

/** How long it took, or has been running for, kept current while it runs. */
function Elapsed({ deployment }: { deployment: Deployment }) {
  const running = RUNNING.has(deployment.status);
  const now = useNow(() => (running ? 1_000 : 3_600_000));
  const started = parseTimestamp(deployment.started_at);
  if (running) return <>{started === null ? "Running" : `Running for ${formatDuration(Math.max(0, (now - started.getTime()) / 1000))}`}</>;
  if (deployment.duration_s === null || deployment.duration_s === undefined) return <span className="text-fg-faint">Not recorded</span>;
  return <span className="mono text-12">{formatDuration(deployment.duration_s)}</span>;
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <dt className="text-12 text-fg-faint">{term}</dt>
      <dd className="flex min-w-0 items-center gap-1.5 text-13 text-fg">{children}</dd>
    </div>
  );
}

function Facts({ deployment }: { deployment: Deployment }) {
  const commit = shortCommit(deployment.git_commit);
  const trigger = triggerWords(deployment.triggered_by);
  const TriggerIcon = trigger.icon;
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-4 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised sm:grid-cols-4">
      <Fact term="Commit">
        {commit ? (
          <div className="flex min-w-0 flex-col gap-1">
            <div className="flex min-w-0 items-center gap-1.5">
              <span translate="no" className="mono text-12">
                {commit}
              </span>
              {deployment.git_branch ? (
                <span translate="no" className="mono truncate text-12 text-fg-muted">
                  {deployment.git_branch}
                </span>
              ) : null}
              <CopyButton value={deployment.git_commit ?? commit} label="Copy commit" className="-my-1" />
            </div>
            {deployment.commit_message ? (
              <p title={deployment.commit_message} className="truncate text-12 text-fg-muted">
                {deployment.commit_message}
              </p>
            ) : null}
          </div>
        ) : (
          <span className="text-fg-faint">Not a git checkout</span>
        )}
      </Fact>
      <Fact term="Started by">
        <TriggerIcon aria-hidden="true" className="size-3.5 shrink-0 text-fg-muted" />
        {trigger.label}
      </Fact>
      <Fact term="Started">
        <RelativeTime value={deployment.started_at} />
      </Fact>
      <Fact term="Duration">
        <Elapsed deployment={deployment} />
      </Fact>
    </dl>
  );
}

function useBuildLog(deployment: Deployment) {
  const running = RUNNING.has(deployment.status);
  const [tail, setTail] = useState<number | null>(null);
  const log = useQuery({
    ...deploymentLogQuery(deployment.id, tail),
    // The recorder flushes every line as it is written, so re-reading the file streams it.
    refetchInterval: running ? 1_000 : false,
  });
  const lines = useLogLines(log.data?.content);
  return { log, lines, tail, setTail };
}

const LOG_ROW_PX = 20;
const LOG_CHROME_PX = 60;
/** The smaller of the usual frames (26rem): a log taller than this gets the usual frame and scrolls. */
const LOG_FRAME_PX = 416;

function LogSection({
  domain,
  deployment,
  lines,
  log,
  fallback,
  wholeLog,
}: {
  domain: string;
  deployment: Deployment;
  lines: readonly LogLine[];
  log: ReturnType<typeof useBuildLog>["log"];
  fallback: readonly LogLine[];
  wholeLog: () => void;
}) {
  const running = RUNNING.has(deployment.status);
  const missing = log.data?.missing_reason ?? null;
  const shown = lines.length > 0 ? lines : fallback;
  // Rows of 20px, the toolbar and the padding: a finished log shorter than the usual frame
  // gets a frame its own height (never under 12rem), instead of a well of empty space.
  const natural = shown.length * LOG_ROW_PX + LOG_CHROME_PX;
  const fitted = !running && natural < LOG_FRAME_PX ? Math.max(natural, 192) : null;
  return (
    <Section
      title="Build log"
      level={3}
      description={running ? "Follows the deploy as it writes. Scrolling up pauses; Follow resumes." : undefined}
      actions={
        log.data?.truncated ? (
          <Button size="sm" variant="ghost" loading={log.isFetching} onClick={wholeLog}>
            Show the whole log
          </Button>
        ) : undefined
      }
    >
      {log.data?.truncated ? (
        <p className="text-12 text-fg-muted">{`The start is not shown: the log is longer than the last ${formatBytes(DEFAULT_TAIL)} the console reads by default.`}</p>
      ) : null}
      {log.isError && log.data === undefined ? (
        <ErrorBlock error={log.error} title="Could not read the build log" onRetry={() => void log.refetch()} retrying={log.isRefetching} />
      ) : log.data === undefined ? (
        <div aria-busy="true" className="h-[26rem] rounded-card border border-border bg-bg-sunken p-4 lg:h-[34rem]">
          <span className="sr-only">Loading the build log</span>
          <div aria-hidden="true" className="flex flex-col gap-2.5">
            {["w-2/3", "w-1/2", "w-3/4", "w-2/5", "w-3/5", "w-1/3"].map((width) => (
              <Skeleton key={width} className={`h-3 ${width}`} />
            ))}
          </div>
        </div>
      ) : missing !== null && shown.length === 0 && !running ? (
        <div className="flex flex-col gap-2 rounded-card border border-dashed border-border px-4 py-4">
          <p className="flex items-center gap-2 text-13 font-medium text-fg">
            <FileX aria-hidden="true" className="size-4 text-fg-faint" />
            No build log for this deploy
          </p>
          <pre className="text-12 whitespace-pre-wrap text-fg-muted">{missing}</pre>
        </div>
      ) : fitted !== null ? (
        // A finished deploy with a short log: the viewer is as tall as what it holds.
        <LogViewer
          lines={shown}
          height={fitted}
          pageSearch
          label={`Build log of deployment ${String(deployment.id)} of ${domain}`}
          filename={`${domain}-deployment-${String(deployment.id)}.log`}
          emptyMessage="The log is empty."
        />
      ) : (
        <div className="h-[26rem] lg:h-[34rem]">
          <LogViewer
            lines={shown}
            height="fill"
            pageSearch
            label={`Build log of deployment ${String(deployment.id)} of ${domain}`}
            filename={`${domain}-deployment-${String(deployment.id)}.log`}
            emptyMessage={running ? "Waiting for the first line." : "The log is empty."}
          />
        </div>
      )}
    </Section>
  );
}

function PageSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-6">
      <span className="sr-only">Loading the deployment</span>
      <div aria-hidden="true" className="flex flex-col gap-6">
        <Skeleton className="h-6 w-56" />
        <Skeleton className="h-16 w-full rounded-card" />
        <Skeleton className="h-20 w-full rounded-card" />
        <Skeleton className="h-[26rem] w-full rounded-card" />
      </div>
    </div>
  );
}

function Back({ domain }: { domain: string }) {
  return (
    <Link to="/apps/$domain/deployments" params={{ domain }} className={`${LINK} inline-flex items-center gap-1 self-start`}>
      <ChevronLeft aria-hidden="true" className="size-4" />
      All deployments
    </Link>
  );
}

function Deploy({ domain, deployment }: { domain: string; deployment: Deployment }) {
  const running = RUNNING.has(deployment.status);
  const outcome = outcomeOf(deployment.status);
  const { log, lines, setTail } = useBuildLog(deployment);
  const job = useDeploymentJob(deployment);
  const app = useQuery(appQuery(domain));
  const now = useNow(() => (running ? 1_000 : 3_600_000));
  // A static site is served as files: nothing answers a health check, so it has none.
  const staticSite = app.data !== undefined && !hasUnit(app.data);

  const events = mergeEvents(buildLogEvents(lines), jobEvents(job.entries));
  const span = logSpan([...lines.map((line) => line.at), ...job.entries.map((entry) => entry.at)]);
  const phases = timeline(
    events,
    outcome,
    { lastAt: span.last, now: new Date(now), offset: logClockOffset(span.first, parseTimestamp(deployment.started_at)) },
    { checksHealth: !staticSite },
  );
  const current = currentPhase(phases);

  // Phase changes, and how it ended, are said once each; log lines never.
  const moment = running ? (current?.key ?? "start") : deployment.status;
  const said = running
    ? `Deployment ${String(deployment.id)}: ${current?.doing.toLowerCase() ?? "starting"}`
    : `Deployment ${String(deployment.id)} ${outcome === "failed" ? "failed" : "finished"}`;
  useAnnounceChange(moment, said, outcome === "failed" ? "assertive" : "polite");

  const fallback: LogLine[] = job.entries.map((entry, index) => ({
    id: index,
    text: entry.text,
    ...(entry.at ? { ts: logClockText(entry.at) } : {}),
  }));
  const failure = outcome === "failed" ? failureWords(current) : null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-4">
        <Back domain={domain} />
        <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
          <div className="flex min-w-0 flex-wrap items-center gap-3">
            <h2 className="title text-18 text-fg">{`Deployment ${String(deployment.id)}`}</h2>
            <DeployStatePill status={deployment.status} />
            {job.socket === "reconnecting" ? <StatusPill state="deploying" label="Reconnecting" appearance="inline" size="sm" /> : null}
          </div>
          <DeploymentActions domain={domain} deployment={deployment} />
        </div>
      </div>

      <Facts deployment={deployment} />

      <div className="rounded-card border border-border bg-surface px-3 py-5 shadow-raised sm:px-6">
        <PhaseTimeline phases={phases} outcome={outcome} />
        {staticSite ? (
          <p className="mt-4 border-t border-border pt-3 text-center text-12 text-pretty text-fg-muted">
            Health does not apply: a static site is served as files by the web server, with no process of its own to probe.
          </p>
        ) : null}
      </div>

      {failure !== null ? (
        <div className="flex flex-col gap-2">
          <ErrorBlock
            error={{ detail: deployment.error ?? "The deploy failed without recording why. The log below shows how far it got." }}
            title={failure.title}
            hint={failure.hint}
          />
          <Link to="/apps/$domain/diagnose" params={{ domain }} className={`${LINK} self-start`}>
            Diagnose this app
          </Link>
        </div>
      ) : null}

      <LogSection
        domain={domain}
        deployment={deployment}
        lines={lines}
        log={log}
        fallback={fallback}
        wholeLog={() => {
          setTail(WHOLE_LOG);
        }}
      />
    </div>
  );
}

/**
 * One deploy: what it built, how far it got phase by phase, its build log streamed while it
 * runs, and if it failed, the error in its own words with what to do about it.
 */
export function DeploymentPage({ domain, id }: { domain: string; id: string }) {
  const numeric = /^\d+$/.test(id) ? Number(id) : null;
  useDocumentTitle(`Deployment ${id} - ${domain}`, 1);
  const deployment = useQuery({
    ...deploymentQuery(numeric ?? 0),
    enabled: numeric !== null,
    // A deploy from the command line has no job to announce its end; this catches it.
    refetchInterval: (query) => (query.state.data && RUNNING.has(query.state.data.status) ? 3_000 : false),
  });

  const missing = numeric === null || (deployment.isError && isApiError(deployment.error) && deployment.error.status === 404);
  if (missing) {
    return (
      <div className="flex flex-col gap-4">
        <Back domain={domain} />
        <EmptyState
          level={2}
          icon={<FileX />}
          title={`No deployment ${id}`}
          description="The history keeps the last twenty deploys of each app; older ones are pruned. Every deploy still recorded is in the list."
          className="py-12"
        />
      </div>
    );
  }
  if (deployment.data === undefined) {
    return deployment.isError ? (
      <ErrorBlock error={deployment.error} title={`Could not load deployment ${id}`} onRetry={() => void deployment.refetch()} retrying={deployment.isRefetching} />
    ) : (
      <PageSkeleton />
    );
  }
  if (deployment.data.domain !== domain) {
    const owner = deployment.data.domain;
    return (
      <div className="flex flex-col gap-4">
        <Back domain={domain} />
        <EmptyState
          level={2}
          icon={<FileX />}
          title={`Deployment ${id} is not one of ${domain}'s`}
          description={
            <>
              {"It deployed "}
              <Link to="/apps/$domain/deployments/$id" params={{ domain: owner, id }} className={LINK}>
                {owner}
              </Link>
              .
            </>
          }
          className="py-12"
        />
      </div>
    );
  }
  return <Deploy domain={domain} deployment={deployment.data} />;
}

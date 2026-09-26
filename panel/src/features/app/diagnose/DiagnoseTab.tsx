import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChevronDown, CircleCheck, CircleHelp, CircleMinus, CircleX, RotateCw, TriangleAlert } from "lucide-react";
import { useId } from "react";
import type { ReactNode } from "react";

import { diagnosisQuery } from "../../../api/queries/apps";
import type { Diagnosis } from "../../../api/queries/apps";
import { announce } from "../../../app/Announcer";
import { useDocumentTitle } from "../../../app/documentTitle";
import { CommandHint } from "../../../components/page/CommandHint";
import { QueryState } from "../../../components/page/QueryState";
import { RelativeTime } from "../../../components/page/RelativeTime";
import { Section, Sections } from "../../../components/page/Section";
import { Button } from "../../../components/ui/Button";
import { Skeleton } from "../../../components/ui/Skeleton";
import { Spinner } from "../../../components/ui/Spinner";
import { SystemOutput } from "../../../components/ui/SystemOutput";
import { cx } from "../../../lib/cx";
import { formatCount } from "../../../lib/format";
import { causeFallback, checkLabel, checkStatus, opensByDefault, tally, verdictAnnouncement, verdictView } from "./view";
import type { Check, Tone } from "./view";

const TONE_TEXT: Record<Tone, string> = { ok: "text-ok", warn: "text-warn", fail: "text-fail", idle: "text-idle" };
const TONE_RAIL: Record<Tone, string> = { ok: "bg-ok", warn: "bg-warn", fail: "bg-fail", idle: "bg-idle" };
const TONE_SOFT: Record<Tone, string> = { ok: "bg-ok-soft", warn: "bg-warn-soft", fail: "bg-fail-soft", idle: "bg-idle-soft" };

const LINK =
  "rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus";

/** A state's shape: a check, a warning triangle, a cross, a dash. Never the colour alone. */
export function ToneIcon({ tone, unknown = false, className }: { tone: Tone; unknown?: boolean; className?: string }) {
  const Icon = unknown ? CircleHelp : tone === "ok" ? CircleCheck : tone === "warn" ? TriangleAlert : tone === "fail" ? CircleX : CircleMinus;
  return <Icon aria-hidden="true" className={cx("shrink-0", TONE_TEXT[tone], className)} />;
}

/** Where to go next from a check, when another tab shows more of the same. */
function nextStep(check: Check, domain: string): ReactNode {
  switch (check.name) {
    case "journal":
      return (
        <Link to="/apps/$domain/logs" params={{ domain }} className={LINK}>
          Follow the live log
        </Link>
      );
    case "last_deployment":
      return (
        <Link to="/apps/$domain/deployments" params={{ domain }} className={LINK}>
          Open the deployments
        </Link>
      );
    case "certificate":
      return (
        <Link to="/apps/$domain/domains" params={{ domain }} className={LINK}>
          Open the domains
        </Link>
      );
    default:
      return null;
  }
}

function Verdict({
  diagnosis,
  checkedAt,
  running,
  onRunAgain,
}: {
  diagnosis: Diagnosis;
  checkedAt: number;
  running: boolean;
  onRunAgain: () => void;
}) {
  const headingId = useId();
  const view = verdictView(diagnosis.verdict);
  const counts = tally(diagnosis.checks);
  const parts = [
    { tone: "ok" as const, count: counts.ok, word: "passed" },
    { tone: "warn" as const, count: counts.warn, word: counts.warn === 1 ? "warning" : "warnings" },
    { tone: "fail" as const, count: counts.fail, word: "failed" },
    { tone: "idle" as const, count: counts.skip, word: "skipped" },
  ].filter((part) => part.count > 0);

  return (
    <section
      aria-labelledby={headingId}
      aria-busy={running || undefined}
      className="relative flex min-w-0 overflow-hidden rounded-card border border-border bg-surface shadow-raised"
    >
      <span aria-hidden="true" className={cx("w-1 shrink-0", TONE_RAIL[view.tone])} />
      <div className="flex min-w-0 flex-1 flex-col gap-4 px-5 py-4 sm:px-6 sm:py-5">
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
          <h2 id={headingId} className="flex items-center gap-2">
            <span className="sr-only">Verdict:</span>{" "}
            <span
              data-verdict={diagnosis.verdict}
              className={cx("inline-flex h-7 items-center gap-1.5 rounded-pill pr-3 pl-2 text-13 font-semibold text-fg", TONE_SOFT[view.tone])}
            >
              <ToneIcon tone={view.tone} unknown={view.tone === "idle"} className="size-4" />
              {view.word}
            </span>
          </h2>
          <div className="flex items-center gap-3">
            <span className="text-12 text-fg-muted">
              {"Checked "}
              <RelativeTime value={checkedAt} />
            </span>
            <Button size="sm" icon={<RotateCw aria-hidden="true" />} loading={running} onClick={onRunAgain}>
              Run again
            </Button>
          </div>
        </div>

        <div className="flex max-w-[72ch] flex-col gap-1">
          <p className="text-12 text-fg-muted">{diagnosis.probable_cause ? "Probable cause" : "No single cause"}</p>
          <p className="title text-18 text-pretty text-fg sm:text-24">{diagnosis.probable_cause ?? causeFallback(diagnosis.verdict)}</p>
        </div>

        <p className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-13 text-fg-muted">
          <span>{`${formatCount(diagnosis.checks.length)} ${diagnosis.checks.length === 1 ? "check" : "checks"}:`}</span>
          {parts.map((part) => (
            <span key={part.word} className="inline-flex items-center gap-1.5">
              <ToneIcon tone={part.tone} className="size-3.5" />
              <span>
                <span className="mono text-fg">{part.count}</span> {part.word}
              </span>
            </span>
          ))}
        </p>
      </div>
    </section>
  );
}

/** The status column of a check row: shape and word, the word in the row's reading order. */
function CheckStatus({ status }: { status: string }) {
  const view = checkStatus(status);
  return (
    <span className="inline-flex h-5 w-24 shrink-0 items-center gap-1.5 text-12 font-medium text-fg-muted">
      <ToneIcon tone={view.tone} className="size-4" />
      <span className={view.tone === "fail" ? "text-fail" : undefined}>{view.word}</span>
    </span>
  );
}

const ROW = "grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-1 px-4 py-3 sm:grid-cols-[6rem_11rem_minmax(0,1fr)_auto]";

function CheckRow({ check, domain }: { check: Check; domain: string }) {
  const evidence = check.evidence.trim() !== "";
  const next = nextStep(check, domain);
  const head = (
    <>
      <CheckStatus status={check.status} />
      <span className="text-13 leading-5 font-medium text-fg">{checkLabel(check.name)}</span>
      <span className="col-span-2 text-13 leading-5 text-pretty text-fg-muted sm:col-span-1">{check.summary}</span>
    </>
  );

  if (!evidence) {
    return (
      <li className={ROW}>
        {head}
        <span className="col-span-2 flex min-h-5 items-center gap-4 text-12 text-fg-faint sm:col-span-1 sm:justify-end">
          {next}
          <span>No output</span>
        </span>
      </li>
    );
  }

  return (
    <li>
      <details open={opensByDefault(check)} className="group">
        <summary
          className={cx(
            ROW,
            "cursor-pointer list-none hover:bg-surface-hover [&::-webkit-details-marker]:hidden",
            "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus",
          )}
        >
          {head}
          <span className="col-span-2 flex min-h-5 items-center gap-1 text-12 text-fg-faint sm:col-span-1 sm:justify-end">
            <span className="group-open:hidden">Show output</span>
            <span className="hidden group-open:inline">Hide output</span>
            <ChevronDown aria-hidden="true" className="size-3.5 transition-transform duration-(--duration-fast) group-open:rotate-180" />
          </span>
        </summary>
        <div className="flex flex-col gap-2 px-4 pb-4 sm:pl-[calc(6rem+2rem)]">
          <SystemOutput
            label={`Output of the ${checkLabel(check.name).toLowerCase()} check`}
            maxHeight="max-h-72"
            className="rounded-control border border-border bg-bg-sunken px-3 py-2 leading-5"
          >
            {check.evidence}
          </SystemOutput>
          {next !== null ? <div>{next}</div> : null}
        </div>
      </details>
    </li>
  );
}

/** The probes a diagnosis runs; the checks list's placeholder holds as many rows. */
const PROBES = 10;

/**
 * The loaded view's shape: the verdict card with its pill row, the cause (a line of the large
 * title, often two) and the tally; then the Checks section with a row per probe. The terminal
 * hint below starts where it will stay, well below the fold.
 */
function DiagnosisSkeleton() {
  return (
    <div className="flex flex-col gap-8">
      <div className="flex overflow-hidden rounded-card border border-border bg-surface shadow-raised">
        <span aria-hidden="true" className="w-1 shrink-0 bg-border" />
        <div className="flex flex-1 flex-col gap-4 px-5 py-4 sm:px-6 sm:py-5">
          <div className="flex min-h-8 items-center gap-2.5 text-13 text-fg-muted">
            <Spinner size={14} className="text-warn" />
            <span>Running the checks. Probing the unit, the port, HTTP, the logs and the certificate takes a few seconds.</span>
          </div>
          <div aria-hidden="true" className="flex max-w-[72ch] flex-col gap-1">
            <div className="flex h-4 items-center">
              <Skeleton className="h-3 w-24" />
            </div>
            <div className="flex h-14 flex-col justify-center gap-2 sm:h-16">
              <Skeleton className="h-5 w-full sm:h-6" />
              <Skeleton className="h-5 w-2/3 sm:h-6" />
            </div>
          </div>
          <div aria-hidden="true" className="flex h-5 items-center">
            <Skeleton className="h-3 w-64" />
          </div>
        </div>
      </div>
      {/* The Checks section's header as it will be drawn, not a second region of that name. */}
      <div aria-hidden="true" className="flex min-w-0 flex-col gap-4">
        <div>
          <p className="title text-16 text-fg">Checks</p>
          <p className="mt-0.5 max-w-[68ch] text-13 text-pretty text-fg-muted">
            Every probe, in the order it ran, with its output as the system printed it.
          </p>
        </div>
        <div className="flex flex-col divide-y divide-border rounded-card border border-border bg-surface shadow-raised">
          {Array.from({ length: PROBES }, (_, i) => (
            <div key={i} className="flex h-11 items-center gap-3 px-4">
              <Skeleton className="h-3 w-16 sm:w-24" />
              <Skeleton className="h-3 w-28 sm:w-44" />
              <Skeleton className={cx("h-3", i % 2 === 0 ? "w-72" : "w-52")} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/**
 * "Why is it down": every probe WASM can run about the app, correlated into a verdict with the
 * probable cause first, and each probe's own output verbatim. Nothing here changes the machine.
 */
export function DiagnoseTab({ domain }: { domain: string }) {
  useDocumentTitle(`Diagnose - ${domain}`, 1);
  // The probes take seconds and answer for now: never on focus, only when asked.
  const diagnosis = useQuery({ ...diagnosisQuery(domain), refetchOnWindowFocus: false });

  const runAgain = (): void => {
    void diagnosis.refetch().then((result) => {
      if (result.data === undefined || result.isError) return;
      announce(verdictAnnouncement(domain, result.data), result.data.verdict === "down" ? "assertive" : "polite");
    });
  };

  return (
    <Sections>
      <section aria-label={`Diagnosis of ${domain}`} className="min-w-0">
        <QueryState query={diagnosis} label={`the diagnosis of ${domain}`} skeleton={<DiagnosisSkeleton />}>
        {(data) => (
          <div className="flex flex-col gap-8">
            <Verdict
              diagnosis={data}
              checkedAt={diagnosis.dataUpdatedAt}
              running={diagnosis.isFetching}
              onRunAgain={runAgain}
            />
            <Section title="Checks" description="Every probe, in the order it ran, with its output as the system printed it.">
              <ul className="flex min-w-0 flex-col divide-y divide-border overflow-hidden rounded-card border border-border bg-surface shadow-raised">
                {data.checks.map((check) => (
                  <CheckRow key={check.name} check={check} domain={domain} />
                ))}
              </ul>
            </Section>
          </div>
        )}
        </QueryState>
      </section>
      <CommandHint command={`wasm diagnose ${domain}`} label="From a terminal" />
    </Sections>
  );
}

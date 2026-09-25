import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { FileText } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";

import { appQuery } from "../../../api/queries/apps";
import { useDocumentTitle } from "../../../app/documentTitle";
import { CommandHint } from "../../../components/page/CommandHint";
import { ErrorBlock } from "../../../components/page/QueryState";
import { appStatus } from "../../../components/page/status";
import { EmptyState } from "../../../components/ui/EmptyState";
import { LogViewer } from "../../../components/ui/LogViewer";
import type { LogLine } from "../../../components/ui/LogViewer";
import { Skeleton } from "../../../components/ui/Skeleton";
import { StatusPill } from "../../../components/ui/StatusPill";
import type { Status } from "../../../components/ui/StatusPill";
import { formatCount } from "../../../lib/format";
import { useLogStream } from "../../../realtime/sockets";
import type { SocketStatus } from "../../../realtime/sockets";
import { levelOf } from "../deployments/buildLog";

/** Most journal lines held on screen; the oldest go first. */
export const LOG_CAP = 10_000;

/** Journal lines asked for when the stream opens. */
const BACKLOG = 200;

const CONNECTION: Readonly<Record<SocketStatus, { state: Status; label: string }>> = {
  connecting: { state: "deploying", label: "Connecting" },
  open: { state: "running", label: "Live" },
  reconnecting: { state: "deploying", label: "Reconnecting" },
  closed: { state: "stopped", label: "Disconnected" },
};

/**
 * A journal line in `short-iso` form: "2026-09-25T21:45:52+0100 web-01 shop[41234]: message".
 * The stamp and the host are systemd's, not the app's: the time goes to the time column, the
 * host (always this machine) is dropped, and the rest stays verbatim from the unit's name on.
 */
const JOURNAL = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2}:\d{2})(?:[+-]\d{2}:?\d{2}|Z)? \S+ (.*)$/;

export function journalLine(line: LogLine): LogLine {
  const match = JOURNAL.exec(line.text);
  const text = match?.[2] ?? line.text;
  const level = line.level ?? levelOf(text);
  if (match === null && level === line.level) return line;
  return { ...line, text, ...(match ? { ts: match[1] ?? "" } : {}), ...(level === undefined ? {} : { level }) };
}

// A line keeps its identity once read, so the viewer's per-line cache keeps working.
const read = new WeakMap<LogLine, LogLine>();

function withLevel(line: LogLine): LogLine {
  let found = read.get(line);
  if (found === undefined) {
    found = journalLine(line);
    read.set(line, found);
  }
  return found;
}

/**
 * The app's journal as systemd writes it: a backlog, then every new line as it arrives. The
 * stream reconnects by itself; the viewer follows the newest line until the operator scrolls
 * up, and `/` searches it.
 */
function Journal({ domain, failed }: { domain: string; failed: boolean }) {
  const stream = useLogStream(domain, { lines: BACKLOG, cap: LOG_CAP });
  const lines = useMemo(() => stream.lines.map(withLevel), [stream.lines]);
  const host = useRef<HTMLDivElement>(null);
  const connection = CONNECTION[stream.status];

  // `/` searches the page's own content first: here, the journal.
  useEffect(() => {
    host.current?.querySelector('input[type="search"]')?.setAttribute("data-page-search", "");
  });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <StatusPill state={connection.state} label={connection.label} size="sm" />
          <span className="text-12 text-fg-muted">
            {stream.status === "connecting" && lines.length === 0
              ? `Reading the last ${String(BACKLOG)} lines of the journal`
              : `${formatCount(lines.length)} ${lines.length === 1 ? "line" : "lines"}`}
          </span>
          {failed ? (
            <span className="text-12 text-fg-muted">
              {"The unit has failed; its last lines usually say why. "}
              <Link
                to="/apps/$domain/diagnose"
                params={{ domain }}
                className="rounded-[4px] font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
              >
                Diagnose
              </Link>
            </span>
          ) : null}
        </div>
        <CommandHint command={`wasm logs ${domain} --follow`} label="From a terminal" />
      </div>

      {stream.error !== null ? (
        <ErrorBlock
          live
          compact
          error={{ detail: stream.error }}
          title="The journal stream failed"
          hint="The console follows the unit with journalctl. Reading it from a terminal shows whether the journal itself answers."
        />
      ) : null}
      {stream.truncated ? (
        <p className="rounded-control border border-border bg-bg-sunken px-3 py-2 text-12 text-fg-muted">
          {`Showing the newest ${formatCount(LOG_CAP)} lines. Older ones were dropped from this view; `}
          <code translate="no" className="text-fg">{`wasm logs ${domain} --lines 50000`}</code>
          {" reads further back."}
        </p>
      ) : null}

      <div ref={host} className="h-[max(24rem,calc(100dvh-22rem))]">
        <LogViewer
          lines={lines}
          height="fill"
          label={`Journal of ${domain}`}
          filename={`${domain}-journal.log`}
          emptyMessage={stream.status === "open" ? "The journal has no lines for this unit yet." : "Connecting to the journal."}
        />
      </div>
    </div>
  );
}

/** The Logs tab: the unit's journal, followed live. A static site has no unit, and says so. */
export function LogsTab({ domain }: { domain: string }) {
  useDocumentTitle(`Logs - ${domain}`, 1);
  const app = useQuery(appQuery(domain));

  // The layout owns the load failure and the not-found page.
  if (app.data === undefined) {
    return app.isError ? null : (
      <div aria-busy="true" className="flex flex-col gap-3">
        <span className="sr-only">Loading the journal</span>
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-[24rem] w-full rounded-card" />
      </div>
    );
  }

  // A site of the static type has no process even when a unit was left behind for it.
  if (appStatus(app.data.status).state === "static" || app.data.app_type === "static") {
    return (
      <EmptyState
        level={2}
        icon={<FileText />}
        title="A static site has no process to log"
        description="The web server serves its files directly, so there is no unit and no journal. Requests to it are in the web server's access log."
        command={`tail -f /var/log/nginx/access.log`}
        className="py-16"
      />
    );
  }

  return <Journal domain={domain} failed={appStatus(app.data.status).state === "failed"} />;
}

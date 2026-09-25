import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Fragment } from "react";

import { sessionQuery } from "../api/queries/auth";
import { machineQuery } from "../api/queries/system";
import type { Machine } from "../api/queries/system";
import { Meter } from "../components/ui/Progress";
import { Skeleton } from "../components/ui/Skeleton";
import { StatusGlyph, StatusPill } from "../components/ui/StatusPill";
import { cx } from "../lib/cx";
import { formatDuration } from "../lib/format";
import { useStreamStatus } from "../realtime/events";

/** The recent one-minute load as a line, scaled to its own peak (at least 1). */
export function sparklinePath(samples: readonly number[], width: number, height: number): string {
  if (samples.length === 0) return "";
  const peak = Math.max(1, ...samples);
  const step = samples.length > 1 ? width / (samples.length - 1) : 0;
  return samples
    .map((value, index) => {
      const x = samples.length > 1 ? index * step : width;
      const y = height - (Math.max(0, value) / peak) * (height - 2) - 1;
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
}

function Divider({ className }: { className?: string }) {
  return <span aria-hidden="true" className={cx("h-6 w-px shrink-0 bg-border", className)} />;
}

function Load({ machine }: { machine: Machine }) {
  const [one, five, fifteen] = machine.load;
  return (
    <div className="flex items-center gap-2" title={`Load average: ${one.toFixed(2)} ${five.toFixed(2)} ${fifteen.toFixed(2)}`}>
      <span className="text-12 text-fg-muted">Load</span>{" "}
      <svg width="48" height="18" viewBox="0 0 48 18" aria-hidden="true" className="shrink-0 text-fg-muted">
        <path d={sparklinePath(machine.load_history, 48, 18)} fill="none" stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" strokeLinecap="round" />
      </svg>
      <span className="mono text-13 text-fg">{one.toFixed(2)}</span>{" "}
      <span className="sr-only">{`over one minute, ${five.toFixed(2)} over five, ${fifteen.toFixed(2)} over fifteen`}</span>
    </div>
  );
}

function UnitTally({ units }: { units: Machine["units"] }) {
  const parts = [
    { state: "running" as const, count: units.running, word: "running", tone: units.running > 0 ? "text-ok" : "text-fg-faint" },
    { state: "failed" as const, count: units.failed, word: "failed", tone: units.failed > 0 ? "text-fail" : "text-fg-faint" },
    { state: "stopped" as const, count: units.stopped, word: "stopped", tone: "text-idle" },
  ];
  return (
    <Link
      to="/services"
      title={`WASM units: ${String(units.running)} running, ${String(units.failed)} failed, ${String(units.stopped)} stopped`}
      className="flex items-center gap-2.5 rounded-control px-1.5 py-1 hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus"
    >
      <span className="text-12 text-fg-muted">Units</span>
      {parts.map((part) => (
        // Text-node spaces between the parts keep the accessible name "Units 9 running 1
        // failed 2 stopped"; between flex items they take no room on screen.
        <Fragment key={part.state}>
          {" "}
          <span className={cx("inline-flex items-center gap-1", part.tone)}>
            <StatusGlyph state={part.state} size={10} />
            <span className={cx("mono text-13", part.count > 0 && part.state === "failed" ? "font-medium" : "text-fg")}>
              {part.count}
            </span>{" "}
            <span className="sr-only">{part.word}</span>
          </span>
        </Fragment>
      ))}
    </Link>
  );
}

/**
 * The instrument readout of this one machine: its name, how long it has been up, load, CPU,
 * memory and disk, and how many of WASM's units are running, failed or stopped. Painted from
 * GET /api/system/machine, then kept current by the `machine` event every five seconds.
 * Deliberately not a live region: numbers that change every five seconds are not news.
 *
 * The strip sizes itself by its own width (a container query), not the viewport's: the
 * sidebar comes and goes, and the hostname must never be the thing that gives way. Readings
 * drop out in order of importance: load, uptime, the unit tally (a failure count stays),
 * then the meters.
 */
export function MachineStrip({ className }: { className?: string }) {
  const stream = useStreamStatus();
  // While the stream is down (a restarting panel, a proxy that buffers it) the strip polls,
  // so it never shows numbers from minutes ago as if they were current.
  const { data: machine, isError } = useQuery({ ...machineQuery(), refetchInterval: stream === "live" ? false : 15_000 });
  const { data: hostname } = useQuery({ ...sessionQuery(), select: (session) => session.hostname });
  const name = machine?.hostname ?? hostname;

  return (
    <div role="group" aria-label="This machine" className={cx("@container min-w-0", className)}>
      <div className="flex items-center gap-2 @min-[26rem]:gap-4">
        <Link
          to="/server"
          className="flex min-w-0 items-baseline gap-2 rounded-control px-1.5 py-1 hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus"
        >
          {name !== undefined ? (
            <span translate="no" className="mono truncate text-13 font-medium text-fg">
              {name}
            </span>
          ) : (
            <Skeleton className="h-3.5 w-28" />
          )}
          {machine ? " " : null}
          {machine ? (
            <span className="mono hidden shrink-0 text-12 text-fg-faint @min-[42rem]:inline">
              up {formatDuration(machine.uptime_s)}
            </span>
          ) : null}
        </Link>

        {machine ? (
          <>
            <div className="hidden shrink-0 items-center gap-4 @min-[50rem]:flex">
              <Divider />
              <Load machine={machine} />
            </div>
            <div className="hidden shrink-0 items-center gap-4 @min-[30rem]:flex">
              <Divider />
              <Meter size="sm" label="CPU" value={machine.cpu_percent} className="w-20" />
              <Meter size="sm" label="Memory" value={machine.memory.percent} className="w-20" />
              <Meter size="sm" label="Disk" value={machine.disk.percent} className="w-20" />
            </div>
            <div className="hidden shrink-0 items-center gap-4 @min-[36rem]:flex">
              <Divider />
              <UnitTally units={machine.units} />
            </div>
            {machine.units.failed > 0 ? (
              <Link
                to="/services"
                className="inline-flex shrink-0 items-center gap-1 rounded-control px-1.5 py-1 text-12 font-medium text-fail hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus @min-[36rem]:hidden"
              >
                <StatusGlyph state="failed" size={10} />
                <span className="mono">{machine.units.failed}</span> <span>failed</span>{" "}
                <span className="sr-only">units</span>
              </Link>
            ) : null}
          </>
        ) : isError ? (
          <span className="hidden truncate text-12 text-fg-faint @min-[26rem]:inline">Machine readings unavailable</span>
        ) : (
          <div aria-hidden="true" className="hidden items-center gap-4 @min-[30rem]:flex">
            <Skeleton className="h-6 w-20" />
            <Skeleton className="h-6 w-20" />
            <Skeleton className="h-6 w-20" />
          </div>
        )}

        {stream === "reconnecting" ? (
          <StatusPill state="deploying" label="Reconnecting" appearance="inline" size="sm" className="shrink-0" />
        ) : null}
      </div>
    </div>
  );
}

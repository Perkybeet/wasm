import { useQuery } from "@tanstack/react-query";
import { CircleCheck, Mail, TriangleAlert, X } from "lucide-react";

import { monitorConfigQuery, monitorStatusQuery, observationsQuery } from "../../api/queries/monitor";
import type { MonitorStatus } from "../../api/queries/monitor";
import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section } from "../../components/page/Section";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { IconButton } from "../../components/ui/IconButton";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusPill } from "../../components/ui/StatusPill";
import { useMonitorActions } from "./useMonitorActions";

/** One open finding: what stood out, and the button that dismisses it. */
function ObservationRow({
  process,
  pid,
  severity,
  signal,
  detail,
  when,
  onAcknowledge,
  pending,
}: {
  process: string;
  pid: number;
  severity: string;
  signal: string;
  detail: string | null | undefined;
  when: string;
  onAcknowledge: () => void;
  pending: boolean;
}) {
  return (
    <li className="flex items-start justify-between gap-3 py-2.5">
      <div className="flex min-w-0 items-start gap-2.5">
        <TriangleAlert aria-hidden="true" className={`mt-0.5 size-3.5 shrink-0 ${severity === "warning" ? "text-warn" : "text-fg-faint"}`} />
        <div className="flex min-w-0 flex-col gap-0.5">
          <p className="text-13 text-fg">
            <span translate="no" className="mono font-medium">
              {process}
            </span>
            <span className="text-fg-faint">{` (pid ${String(pid)}) - ${signal}`}</span>
          </p>
          {detail ? <p className="text-12 text-fg-muted">{detail}</p> : null}
          <RelativeTime value={when} className="text-12 text-fg-faint" />
        </div>
      </div>
      <IconButton label={`Acknowledge finding about ${process}`} icon={<X />} size="sm" disabled={pending} onClick={onAcknowledge} />
    </li>
  );
}

/** The unit's state and the buttons for whatever it needs next: install, enable, start. */
function UnitRow({ status }: { status: MonitorStatus }) {
  const { install, enable, disable, start, stop } = useMonitorActions();
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2.5">
        {status.installed ? (
          <StatusPill
            state={status.active ? "running" : "stopped"}
            label={status.active ? "Running" : status.enabled ? "Stopped" : "Installed, not running"}
          />
        ) : (
          <Badge>Not installed</Badge>
        )}
        {status.uptime ? <span className="text-12 text-fg-faint">Since {status.uptime}</span> : null}
      </div>
      <div className="flex items-center gap-2">
        {!status.installed ? (
          <Button size="sm" variant="primary" loading={install.isPending} onClick={() => install.mutate()}>
            Install
          </Button>
        ) : (
          <>
            {status.enabled ? (
              <Button size="sm" disabled={disable.isPending} onClick={() => disable.mutate()}>
                Disable
              </Button>
            ) : (
              <Button size="sm" loading={enable.isPending} onClick={() => enable.mutate()}>
                Enable
              </Button>
            )}
            {status.active ? (
              <Button size="sm" disabled={stop.isPending} onClick={() => stop.mutate()}>
                Stop
              </Button>
            ) : (
              <Button size="sm" loading={start.isPending} onClick={() => start.mutate()}>
                Start
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * The resource monitor: whether its unit is installed and running, its open findings with a
 * way to dismiss them, and a button to prove the email channel works.
 */
export function MonitorCard() {
  const status = useQuery(monitorStatusQuery());
  const config = useQuery(monitorConfigQuery());
  const observations = useQuery(observationsQuery(false));
  const { testEmail, acknowledge } = useMonitorActions();

  return (
    <Section
      title="Resource monitor"
      description="Watches CPU, memory and the units WASM manages, and writes down what stands out. It never acts on a process."
      actions={
        status.data?.installed ? (
          <Button size="sm" icon={<Mail aria-hidden="true" />} loading={testEmail.isPending} onClick={() => testEmail.mutate()}>
            Send test email
          </Button>
        ) : undefined
      }
    >
      {status.isError && status.data === undefined ? (
        <ErrorBlock compact error={status.error} title="Could not read the monitor's status" onRetry={() => void status.refetch()} />
      ) : status.data === undefined ? (
        <Skeleton className="h-24 w-full rounded-card" />
      ) : (
        <div className="flex flex-col gap-4 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
          <UnitRow status={status.data} />

          {config.data ? (
            <p className="text-12 text-fg-muted">
              {`Scans every ${String(config.data.scan_interval)}s; flags CPU above ${String(config.data.cpu_threshold)}% or memory above ${String(config.data.memory_threshold)}%. `}
              {config.data.notify ? "Findings are emailed." : "Email notifications are off."}
            </p>
          ) : null}

          <div>
            <h3 className="mb-1 text-13 font-medium text-fg">Open findings</h3>
            {observations.isError && observations.data === undefined ? (
              <ErrorBlock compact error={observations.error} title="Could not load findings" onRetry={() => void observations.refetch()} />
            ) : observations.data === undefined ? (
              <Skeleton className="h-12 w-full" />
            ) : observations.data.observations.length === 0 ? (
              <p className="flex items-center gap-2 text-13 text-fg-muted">
                <CircleCheck aria-hidden="true" className="size-3.5 text-ok" />
                Nothing open right now.
              </p>
            ) : (
              <ul className="flex flex-col divide-y divide-border">
                {observations.data.observations.map((observation) => (
                  <ObservationRow
                    key={observation.id ?? `${observation.process_name}-${String(observation.pid)}`}
                    process={observation.process_name}
                    pid={observation.pid}
                    severity={observation.severity}
                    signal={observation.signal}
                    detail={observation.detail}
                    when={observation.observed_at}
                    pending={acknowledge.isPending}
                    onAcknowledge={() => {
                      if (observation.id !== null && observation.id !== undefined) acknowledge.mutate(observation.id);
                    }}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </Section>
  );
}

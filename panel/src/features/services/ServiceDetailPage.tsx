import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Cog, MoreHorizontal, Play, RotateCw, Square, Trash2 } from "lucide-react";
import { useState } from "react";

import { isApiError } from "../../api/client";
import { serviceQuery, servicesQuery } from "../../api/queries/services";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { DangerAction, DangerZone } from "../../components/page/DangerZone";
import { KeyValueList, KeyValueListSkeleton } from "../../components/page/KeyValueList";
import type { KeyValueItem } from "../../components/page/KeyValueList";
import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section } from "../../components/page/Section";
import { Button, buttonClassName } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { Dialog } from "../../components/ui/Dialog";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { LogViewer } from "../../components/ui/LogViewer";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusPill } from "../../components/ui/StatusPill";
import { formatBytes } from "../../lib/format";
import { useLogStream } from "../../realtime/sockets";
import { UnitEditor } from "./UnitEditor";
import type { ServiceInfo } from "./data";
import { serviceState } from "./data";
import { useServiceActions } from "./useServiceActions";

const BREADCRUMBS = [{ label: "Services", to: "/services" }] as const;

function NotFound({ name }: { name: string }) {
  return (
    <>
      <PageHeader title={name} breadcrumbs={BREADCRUMBS} />
      <EmptyState
        level={2}
        icon={<Cog />}
        title="No service by this name"
        description="It may have been deleted, or the name has a typo. Every unit WASM manages is in the services list."
        action={
          <Link to="/services" className={buttonClassName("secondary")}>
            All services
          </Link>
        }
        command="wasm service list"
        className="py-16"
      />
    </>
  );
}

/**
 * A unit that exists on this machine but that WASM did not create: found only through the
 * show-all-units listing (`GET /api/services?wasm_only=false`), since the per-name read
 * (`GET /api/services/{name}`) only ever answers what the store tracks and 404s for it.
 * Read-only - no editor, no actions, nothing destructive - the way a foreign row's own menu
 * is already withheld in the list (`ServiceRowActions`).
 */
function ForeignUnit({ name, service }: { name: string; service: ServiceInfo }) {
  const view = serviceState(service);
  return (
    <>
      <PageHeader
        title={name}
        breadcrumbs={BREADCRUMBS}
        description={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <StatusPill state={view.state} label={view.label} />
            {view.detail !== undefined ? <span className="mono text-13 text-fg-muted">{view.detail}</span> : null}
          </span>
        }
      />
      <EmptyState
        level={2}
        icon={<Cog />}
        title="WASM did not create this unit"
        description="It runs on this machine, but its unit file belongs to another package. WASM only manages what it created, so nothing here can edit, restart or delete it."
        command={`systemctl status ${name}`}
        className="py-16"
      />
    </>
  );
}

/** The live journal of the unit, followed while the page is open. */
function ServiceLogs({ name }: { name: string }) {
  const stream = useLogStream(name);
  const reconnecting = stream.status === "reconnecting";
  return (
    <Section
      title="Logs"
      description="The systemd journal as it is written."
      actions={
        <span role="status" className="flex items-center gap-1.5 text-12 text-fg-muted">
          <span
            aria-hidden="true"
            className={`size-1.5 rounded-pill ${stream.status === "open" ? "bg-ok" : reconnecting ? "bg-warn" : "bg-fg-faint"}`}
          />
          {stream.status === "open" ? "Live" : reconnecting ? "Reconnecting" : "Connecting"}
        </span>
      }
    >
      {stream.error !== null ? <ErrorBlock compact error={{ detail: stream.error }} title="The log stream failed" /> : null}
      <LogViewer lines={stream.lines} label={`Logs for ${name}`} filename={`${name}.log`} height={360} pageSearch />
      {stream.truncated ? <p className="text-12 text-fg-faint">Older lines were dropped to stay under 10,000 lines.</p> : null}
    </Section>
  );
}

/**
 * One systemd unit: its state, the actions that act on it directly (no job queue - these are
 * synchronous systemctl calls), its live journal, its raw unit file, and deleting it.
 */
export function ServiceDetailPage({ name }: { name: string }) {
  const service = useQuery(serviceQuery(name));
  const { start, stop, restart, enable, disable, remove } = useServiceActions(name);
  const [confirmStop, setConfirmStop] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const navigate = useNavigate();

  // `GET /api/services/{name}` only ever answers what the store tracks and 404s for a unit
  // WASM did not create; the all-units listing is the only way to tell that unit apart from
  // one that never existed at all, so it is fetched only once the plain read has 404d.
  const notFound = service.isError && isApiError(service.error) && service.error.status === 404;
  const allServices = useQuery({ ...servicesQuery(false), enabled: notFound });
  const foreign = notFound ? allServices.data?.services.find((candidate) => candidate.name === name) : undefined;

  if (notFound) {
    if (foreign) return <ForeignUnit name={name} service={foreign} />;
    if (allServices.isPending) return <PageHeader title={name} breadcrumbs={BREADCRUMBS} description={<Skeleton className="h-6 w-20 rounded-pill" />} />;
    return <NotFound name={name} />;
  }

  const data = service.data;
  const state = data ? serviceState(data) : null;

  const items: KeyValueItem[] = data
    ? [
        { label: "Command", value: data.description ?? null },
        { label: "Main PID", value: data.active && data.pid ? data.pid : null },
        { label: "Memory", value: data.memory ? formatBytes(Number(data.memory)) : null, mono: false, copy: false },
        {
          label: "Since",
          value: data.active && data.uptime ? <RelativeTime value={data.uptime} /> : "Not running",
          mono: false,
          copy: false,
        },
        { label: "Starts at boot", value: data.enabled ? "Yes" : "No", mono: false, copy: false },
        { label: "Managed by", value: "WASM", mono: false, copy: false },
      ]
    : [];

  return (
    <>
      <PageHeader
        title={name}
        breadcrumbs={BREADCRUMBS}
        description={
          data && state ? (
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <StatusPill state={state.state} label={state.label} />
              {state.detail !== undefined ? <span className="mono text-13 text-fg-muted">{state.detail}</span> : null}
            </span>
          ) : (
            <Skeleton className="h-6 w-20 rounded-pill" />
          )
        }
        actions={
          data ? (
            <div className="flex items-center gap-2">
              {data.active ? (
                <Button icon={<Square aria-hidden="true" />} disabled={stop.isPending} onClick={() => setConfirmStop(true)}>
                  Stop
                </Button>
              ) : (
                <Button icon={<Play aria-hidden="true" />} loading={start.isPending} onClick={() => start.mutate()}>
                  Start
                </Button>
              )}
              <Button variant="primary" icon={<RotateCw aria-hidden="true" />} loading={restart.isPending} onClick={() => restart.mutate()}>
                Restart
              </Button>
              <Menu align="end" trigger={<IconButton variant="secondary" label="More actions" icon={<MoreHorizontal />} tooltip={false} />}>
                {data.enabled ? (
                  <MenuItem disabled={disable.isPending} onClick={() => disable.mutate()}>
                    Disable at boot
                  </MenuItem>
                ) : (
                  <MenuItem disabled={enable.isPending} onClick={() => enable.mutate()}>
                    Enable at boot
                  </MenuItem>
                )}
              </Menu>
            </div>
          ) : undefined
        }
      />

      <div className="-mt-4 mb-8 flex flex-col gap-4">
        {service.isError && service.data === undefined ? (
          <ErrorBlock error={service.error} title={`Could not load ${name}`} onRetry={() => void service.refetch()} retrying={service.isRefetching} />
        ) : null}
      </div>

      {data === undefined ? (
        <div aria-busy="true" className="flex flex-col gap-8">
          <span className="sr-only">Loading the service</span>
          <div aria-hidden="true" className="rounded-card border border-border bg-surface px-4 py-1">
            <KeyValueListSkeleton rows={4} />
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-8">
          <Section title="Overview">
            <div className="rounded-card border border-border bg-surface px-4 py-1">
              <KeyValueList items={items} />
            </div>
            <CommandHint command={`wasm service status ${name}`} label="From a terminal" />
          </Section>

          <ServiceLogs name={name} />

          <UnitEditor name={name} />

          <DangerZone>
            <DangerAction
              title="Delete this service"
              description="Stops the unit, disables it and removes its unit file. This cannot be undone."
              action={
                <Button variant="danger" icon={<Trash2 aria-hidden="true" />} onClick={() => setDeleteOpen(true)}>
                  Delete service
                </Button>
              }
            />
          </DangerZone>
        </div>
      )}

      <Dialog
        open={confirmStop}
        onOpenChange={setConfirmStop}
        size="sm"
        title={`Stop ${name}?`}
        description="The unit stops running. Nothing is deleted, and it starts again on the next restart or boot if it is enabled."
        footer={
          <>
            <Button onClick={() => setConfirmStop(false)}>Cancel</Button>
            <Button
              variant="danger"
              loading={stop.isPending}
              onClick={() =>
                stop.mutate(undefined, {
                  onSettled: () => {
                    setConfirmStop(false);
                  },
                })
              }
            >
              Stop service
            </Button>
          </>
        }
      />

      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title={`Delete ${name}`}
        description="Stops the unit, disables it and removes its unit file. This cannot be undone."
        confirmText={name}
        actionLabel="Delete service"
        onConfirm={async () => {
          await remove.mutateAsync();
          void navigate({ to: "/services" });
        }}
      />
    </>
  );
}

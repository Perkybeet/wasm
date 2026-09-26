import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { networkQuery, processesQuery, systemHealthQuery, systemInfoQuery, versionQuery } from "../../api/queries/system";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { StatTile } from "../../components/page/StatTile";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { Select } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusGlyph, StatusPill, stateTextClass } from "../../components/ui/StatusPill";
import { formatBytes, formatPercent } from "../../lib/format";
import { MonitorCard } from "./MonitorCard";
import { checkName, checkView, verdictView } from "./data";
import type { HealthCheck } from "./data";

/** The checks `wasm health` runs on a typical machine, for the placeholder's height. */
const TYPICAL_CHECKS = 6;
/** Mounts a server usually has: the root, a boot partition, a data volume. */
const TYPICAL_DISKS = 3;
/** The loopback and one network card. */
const TYPICAL_INTERFACES = 2;
/** The processes the table asks for; a machine always runs at least that many. */
const PROCESS_LIMIT = 25;

function HealthChecks({ checks }: { checks: readonly HealthCheck[] }) {
  return (
    <ul className="flex flex-col divide-y divide-border">
      {checks.map((check) => {
        const view = checkView(check.status);
        return (
          <li key={check.name} className="flex items-start justify-between gap-3 py-2">
            <span className="flex shrink-0 items-center gap-2 text-13 whitespace-nowrap text-fg">
              <StatusGlyph state={view.state} size={10} className={stateTextClass(view.state)} />
              {checkName(check.name)}
            </span>
            <span className="min-w-0 text-right text-13 text-pretty text-fg-muted">{check.value}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** The loaded card's shape: the verdict's pill, then one row per check. */
function HealthSkeleton() {
  return (
    <div aria-busy="true" className="rounded-card border border-border bg-surface px-4 shadow-raised">
      <span className="sr-only">Running the health check</span>
      <div aria-hidden="true">
        <div className="flex h-12 items-center border-b border-border">
          <Skeleton className="h-6 w-20 rounded-pill" />
        </div>
        <div className="flex flex-col divide-y divide-border">
          {Array.from({ length: TYPICAL_CHECKS }, (_, index) => (
            <div key={index} className="flex h-9 items-center justify-between gap-3">
              <Skeleton className="h-3 w-28" />
              <Skeleton className="h-3 w-40" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Health() {
  const health = useQuery(systemHealthQuery());
  return (
    <Section title="Health" description="The same checks `wasm health` runs.">
      {health.isError && health.data === undefined ? (
        <ErrorBlock compact error={health.error} title="Could not run the health check" onRetry={() => void health.refetch()} />
      ) : health.data === undefined ? (
        <HealthSkeleton />
      ) : (
        <div className="rounded-card border border-border bg-surface px-4 shadow-raised">
          <div className="flex items-center gap-2 border-b border-border py-3">
            <StatusPill state={verdictView(health.data.verdict).state} label={verdictView(health.data.verdict).label} />
          </div>
          <HealthChecks checks={health.data.checks} />
        </div>
      )}
      <CommandHint command="wasm health" label="From a terminal" />
    </Section>
  );
}

/** The installed version, and the one released if a check found a newer one. */
function VersionTile() {
  const version = useQuery(versionQuery());
  if (version.data === undefined) return <StatTile label="Version" value={<Skeleton className="h-5 w-16" />} />;
  const { current_version, has_update, latest_version } = version.data;
  return (
    <StatTile
      label="Version"
      value={current_version}
      mono
      detail={has_update && latest_version ? `Update available: v${latest_version}` : "Up to date"}
    />
  );
}

function SystemInfo() {
  const info = useQuery(systemInfoQuery());
  if (info.isError && info.data === undefined) {
    return (
      <Section title="System">
        <ErrorBlock compact error={info.error} title="Could not read system information" onRetry={() => void info.refetch()} />
      </Section>
    );
  }
  if (info.data === undefined) {
    // The tiles themselves with placeholder readings, and the disks table's own placeholder:
    // the loaded section's shape, so what follows it does not move when the answer lands.
    const pending = <Skeleton className="h-4 w-20" />;
    return (
      <Section title="System">
        <div aria-busy="true" className="flex flex-col gap-4">
          <span className="sr-only">Loading system information</span>
          <div aria-hidden="true" className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-5">
            <StatTile label="Host" value={pending} detail={<Skeleton className="my-0.5 h-3 w-24" />} className="col-span-2 lg:col-span-1" />
            {["Kernel", "CPU", "Memory", "Version"].map((label) => (
              <StatTile key={label} label={label} value={pending} detail={<Skeleton className="my-0.5 h-3 w-24" />} />
            ))}
          </div>
          <Disks disks={[]} loading />
        </div>
      </Section>
    );
  }
  const { hostname, os, kernel, uptime, cpu, memory, disks } = info.data;
  return (
    <Section title="System">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-5">
        {/* Two columns on a phone make five tiles an orphan: the host takes a row of its own. */}
        <StatTile label="Host" value={hostname} mono detail={os} className="col-span-2 lg:col-span-1" />
        <StatTile label="Kernel" value={kernel} mono detail={`Up ${uptime}`} />
        <StatTile
          label="CPU"
          value={formatPercent(cpu.percent)}
          detail={`${String(cpu.cores)} cores, load ${cpu.load_1min.toFixed(2)} ${cpu.load_5min.toFixed(2)} ${cpu.load_15min.toFixed(2)}`}
        />
        <StatTile
          label="Memory"
          value={formatPercent(memory.percent_used)}
          detail={`${formatBytes(memory.used_gb * 1024 ** 3)} of ${formatBytes(memory.total_gb * 1024 ** 3)}`}
        />
        <VersionTile />
      </div>
      <Disks disks={disks} />
    </Section>
  );
}

function Disks({
  disks,
  loading = false,
}: {
  disks: readonly { device: string; mount_point: string; total_gb: number; used_gb: number; percent_used: number }[];
  loading?: boolean;
}) {
  const columns: Column<(typeof disks)[number]>[] = [
    {
      id: "mount",
      header: "Mount",
      mono: true,
      // Container runtimes mount paths a hundred characters long; the row keeps its numbers in
      // view and the whole path stays in the text (and on hover).
      cell: (row) => (
        <span title={row.mount_point} className="block max-w-[10rem] truncate sm:max-w-[28rem]">
          {row.mount_point}
        </span>
      ),
      sortValue: (row) => row.mount_point,
    },
    { id: "device", header: "Device", mono: true, hideBelow: "sm", cell: (row) => row.device, sortValue: (row) => row.device },
    {
      id: "used",
      header: "Used",
      align: "end",
      mono: true,
      cell: (row) => formatBytes(row.used_gb * 1024 ** 3),
      sortValue: (row) => row.used_gb,
    },
    {
      id: "total",
      header: "Total",
      align: "end",
      mono: true,
      hideBelow: "sm",
      cell: (row) => formatBytes(row.total_gb * 1024 ** 3),
      sortValue: (row) => row.total_gb,
    },
    {
      id: "percent",
      header: "Use",
      align: "end",
      mono: true,
      cell: (row) => formatPercent(row.percent_used),
      sortValue: (row) => row.percent_used,
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={disks}
      getRowId={(row) => row.mount_point}
      caption="Disks"
      loading={loading}
      skeletonRows={TYPICAL_DISKS}
      defaultSort={{ column: "mount", direction: "ascending" }}
      empty={<p className="p-4 text-13 text-fg-muted">No mounted filesystem could be read.</p>}
    />
  );
}

function Network() {
  const network = useQuery(networkQuery());
  const interfaces = network.data?.interfaces ?? [];
  const columns: Column<(typeof interfaces)[number]>[] = [
    {
      id: "name",
      header: "Interface",
      mono: true,
      cell: (row) => (
        <span className="flex items-center gap-2">
          <StatusGlyph state={row.is_up ? "running" : "stopped"} size={10} className={row.is_up ? "text-ok" : "text-fg-faint"} />
          {row.name}
        </span>
      ),
      sortValue: (row) => row.name,
    },
    {
      id: "addresses",
      header: "Addresses",
      mono: true,
      hideBelow: "sm",
      cell: (row) => ((row.addresses?.length ?? 0) > 0 ? (row.addresses ?? []).map((a) => a.address).join(", ") : "-"),
    },
    { id: "sent", header: "Sent", align: "end", mono: true, hideBelow: "md", cell: (row) => formatBytes(row.bytes_sent), sortValue: (row) => row.bytes_sent },
    { id: "recv", header: "Received", align: "end", mono: true, hideBelow: "md", cell: (row) => formatBytes(row.bytes_recv), sortValue: (row) => row.bytes_recv },
  ];
  return (
    <Section title="Network">
      {network.isError && network.data === undefined ? (
        <ErrorBlock compact error={network.error} title="Could not read network interfaces" onRetry={() => void network.refetch()} />
      ) : (
        <DataTable
          columns={columns}
          rows={interfaces}
          getRowId={(row) => row.name}
          caption="Network interfaces"
          loading={network.isPending}
          skeletonRows={TYPICAL_INTERFACES}
          defaultSort={{ column: "name", direction: "ascending" }}
          empty={<p className="p-4 text-13 text-fg-muted">No network interface was reported.</p>}
        />
      )}
    </Section>
  );
}

type SortBy = "cpu" | "memory" | "pid" | "name";

const SORT_OPTIONS: readonly { value: SortBy; label: string }[] = [
  { value: "cpu", label: "By CPU" },
  { value: "memory", label: "By memory" },
  { value: "pid", label: "By PID" },
  { value: "name", label: "By name" },
];

function Processes() {
  const [sortBy, setSortBy] = useState<SortBy>("cpu");
  const processes = useQuery(processesQuery(sortBy, PROCESS_LIMIT));
  const rows = processes.data?.processes ?? [];
  const columns: Column<(typeof rows)[number]>[] = [
    { id: "pid", header: "PID", mono: true, width: "w-16", cell: (row) => row.pid },
    { id: "name", header: "Process", mono: true, cell: (row) => row.name },
    { id: "user", header: "User", mono: true, hideBelow: "sm", cell: (row) => row.user },
    { id: "cpu", header: "CPU", align: "end", mono: true, cell: (row) => formatPercent(row.cpu_percent) },
    {
      id: "memory",
      header: "Memory",
      align: "end",
      mono: true,
      hideBelow: "sm",
      cell: (row) => (
        <span>
          {formatBytes(row.memory_mb * 1024 ** 2)} <span className="text-fg-faint">{formatPercent(row.memory_percent)}</span>
        </span>
      ),
    },
  ];
  return (
    <Section
      title="Top processes"
      description="Read-only: the panel does not signal a process."
      actions={
        <Select
          aria-label="Sort processes by"
          size="sm"
          value={sortBy}
          onValueChange={setSortBy}
          options={SORT_OPTIONS}
        />
      }
    >
      {processes.isError && processes.data === undefined ? (
        <ErrorBlock compact error={processes.error} title="Could not list processes" onRetry={() => void processes.refetch()} />
      ) : (
        <DataTable
          columns={columns}
          rows={rows}
          getRowId={(row) => String(row.pid)}
          // Distinct from the Section's own title: a <section> with an accessible name and a
          // region inside it named identically are two landmarks of the same name, which axe's
          // landmark-unique rule (and a screen reader's landmarks list) flags as a duplicate.
          caption="Processes by resource use"
          loading={processes.isPending}
          skeletonRows={PROCESS_LIMIT}
          empty={<p className="p-4 text-13 text-fg-muted">No process was reported.</p>}
        />
      )}
    </Section>
  );
}

/** Health, hardware and processes of this machine, and the resource monitor's own controls. */
export function ServerPage() {
  return (
    <>
      <PageHeader title="Server" description="Health, hardware and processes of this machine." />
      <div className="flex flex-col gap-8">
        <Health />
        <SystemInfo />
        <Network />
        <Processes />
        <MonitorCard />
      </div>
    </>
  );
}

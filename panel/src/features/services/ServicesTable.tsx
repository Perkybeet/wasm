import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { RelativeTime } from "../../components/page/RelativeTime";
import { STATE_RANK } from "../../components/page/status";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { StatusPill } from "../../components/ui/StatusPill";
import { formatBytes } from "../../lib/format";
import type { ServiceInfo } from "./data";
import { serviceState } from "./data";

/** A table cell with nothing to report: a dash on screen, the reason for screen readers. */
function Nothing({ reason }: { reason: string }) {
  return (
    <>
      <span aria-hidden="true" className="text-fg-faint">
        -
      </span>
      <span className="sr-only">{reason}</span>
    </>
  );
}

function memoryOf(service: ServiceInfo): number | null {
  if (!service.memory) return null;
  const bytes = Number(service.memory);
  return Number.isFinite(bytes) && bytes > 0 ? bytes : null;
}

export interface ServicesTableProps {
  services: readonly ServiceInfo[];
  caption: string;
  loading?: boolean;
  empty?: ReactNode;
  rowActions?: (service: ServiceInfo) => ReactNode;
  className?: string;
}

/**
 * Systemd units: their state, whether they start at boot and their live readings. WASM's own
 * by default; with every unit listed, a column says which are WASM's and which are foreign
 * (read-only: see `./data`).
 */
export function ServicesTable({ services, caption, loading = false, empty, rowActions, className }: ServicesTableProps) {
  // Only worth a column when the list mixes both: otherwise every row would say "WASM".
  const mixed = services.some((service) => !service.managed);
  const columns: Column<ServiceInfo>[] = [
    {
      id: "state",
      header: "State",
      width: "w-28",
      cell: (row) => {
        const view = serviceState(row);
        return (
          <span className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
            <StatusPill state={view.state} label={view.label} appearance="inline" size="sm" />
            {view.detail !== undefined ? <span className="mono text-12 text-fg-faint">{view.detail}</span> : null}
          </span>
        );
      },
      sortValue: (row) => STATE_RANK[serviceState(row).state],
    },
    {
      id: "name",
      header: "Unit",
      cell: (row) => (
        <span className="flex min-w-0 items-baseline gap-2">
          <Link
            to="/services/$name"
            params={{ name: row.name }}
            translate="no"
            className="-mx-1 rounded-[4px] px-1 py-0.5 font-medium text-fg mono text-12 hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
          >
            {row.name}
          </Link>
          {/* On a phone the Managed column is hidden: a foreign unit still says so. */}
          {row.managed ? null : <span className="text-12 text-fg-muted sm:hidden">Foreign</span>}
        </span>
      ),
      sortValue: (row) => row.name,
    },
    {
      id: "description",
      header: "Command",
      mono: true,
      hideBelow: "md",
      cell: (row) => (row.description ? <span className="truncate text-fg-muted" title={row.description}>{row.description}</span> : <Nothing reason="No command recorded" />),
      sortValue: (row) => row.description ?? null,
    },
    ...(mixed
      ? [
          {
            id: "managed",
            header: "Managed",
            width: "w-28",
            hideBelow: "sm" as const,
            // Words, not colour: nothing here is a state or something to act on.
            cell: (row: ServiceInfo) => <span className={row.managed ? "text-fg" : "text-fg-muted"}>{row.managed ? "WASM" : "Foreign"}</span>,
            sortValue: (row: ServiceInfo) => (row.managed ? 0 : 1),
          },
        ]
      : []),
    {
      id: "enabled",
      header: "Boot",
      width: "w-20",
      hideBelow: "sm",
      cell: (row) => <span className="text-fg-muted">{row.enabled ? "Enabled" : "Disabled"}</span>,
      sortValue: (row) => (row.enabled ? 0 : 1),
    },
    {
      id: "uptime",
      header: "Since",
      width: "w-32",
      hideBelow: "lg",
      cell: (row) => (row.active && row.uptime ? <RelativeTime value={row.uptime} /> : <Nothing reason="Not running" />),
    },
    {
      id: "memory",
      header: "Memory",
      align: "end",
      mono: true,
      width: "w-24",
      hideBelow: "lg",
      cell: (row) => {
        const bytes = memoryOf(row);
        return bytes === null ? <Nothing reason="No reading" /> : formatBytes(bytes);
      },
      sortValue: (row) => memoryOf(row),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={services}
      getRowId={(row) => row.name}
      caption={caption}
      loading={loading}
      {...(empty !== undefined ? { empty } : {})}
      {...(rowActions ? { rowActions } : {})}
      defaultSort={{ column: "name", direction: "ascending" }}
      {...(className !== undefined ? { className } : {})}
    />
  );
}

import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import { RelativeTime } from "../../components/page/RelativeTime";
import { STATE_RANK } from "../../components/page/status";
import { Badge } from "../../components/ui/Badge";
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
 * Every systemd unit WASM created: its state, whether it starts at boot and its live
 * readings. Every row is WASM-managed - see the module docstring in `./data` for why a
 * foreign unit cannot appear here today.
 */
export function ServicesTable({ services, caption, loading = false, empty, rowActions, className }: ServicesTableProps) {
  const columns: Column<ServiceInfo>[] = [
    {
      id: "state",
      header: "State",
      width: "w-28",
      cell: (row) => {
        const state = serviceState(row);
        return <StatusPill state={state} label={state === "running" ? "Running" : "Stopped"} appearance="inline" size="sm" />;
      },
      sortValue: (row) => STATE_RANK[serviceState(row)],
    },
    {
      id: "name",
      header: "Unit",
      cell: (row) => (
        <Link
          to="/services/$name"
          params={{ name: row.name }}
          translate="no"
          className="-mx-1 rounded-[4px] px-1 py-0.5 font-medium text-fg mono text-12 hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
        >
          {row.name}
        </Link>
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
    {
      id: "managed",
      header: "Managed",
      width: "w-28",
      hideBelow: "sm",
      // Every row this endpoint can return is WASM's own; see ./data for the gap that keeps a
      // foreign unit from ever appearing here to contrast it against.
      cell: () => <Badge tone="accent">WASM</Badge>,
    },
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

import { Link, useNavigate } from "@tanstack/react-router";
import { MoreHorizontal, SquareTerminal, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";

import type { Database } from "../../api/queries/databases";
import { Badge } from "../../components/ui/Badge";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { formatCount } from "../../lib/format";
import { engineLabel } from "./data";
import { useDatabaseActions } from "./useDatabaseActions";

/** One row of the databases table: the engine's own report, name unique per engine. */
export type DatabaseRow = Database;

function RowActions({ database }: { database: DatabaseRow }) {
  const navigate = useNavigate();
  const { dropDatabase } = useDatabaseActions();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const keysLabel = database.engine === "redis" ? "keys" : "tables";
  return (
    <>
      <Menu
        align="end"
        trigger={<IconButton label={`Actions for ${database.name}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}
      >
        <MenuItem
          icon={<SquareTerminal />}
          onClick={() =>
            void navigate({ to: "/databases/$engine/$name", params: { engine: database.engine, name: database.name } })
          }
        >
          Open
        </MenuItem>
        <MenuItem icon={<Trash2 />} destructive onClick={() => setConfirmOpen(true)}>
          Drop database
        </MenuItem>
      </Menu>
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Drop ${database.name}`}
        description={`This permanently deletes the '${database.name}' ${keysLabel === "keys" ? "slot" : "database"} on ${engineLabel(database.engine)} and everything in it. This cannot be undone.`}
        confirmText={database.name}
        actionLabel="Drop database"
        onConfirm={async () => {
          await dropDatabase.mutateAsync({ engine: database.engine, name: database.name });
        }}
      />
    </>
  );
}

export interface DatabasesTableProps {
  databases: readonly DatabaseRow[];
  caption: string;
  loading?: boolean;
  empty?: ReactNode;
  className?: string;
}

/** Every database an installed, running engine reports, across engines unless filtered. */
export function DatabasesTable({ databases, caption, loading = false, empty, className }: DatabasesTableProps) {
  const columns: Column<DatabaseRow>[] = [
    {
      id: "engine",
      header: "Engine",
      width: "w-36",
      cell: (row) => <Badge tone="neutral">{engineLabel(row.engine)}</Badge>,
      sortValue: (row) => row.engine,
    },
    {
      id: "name",
      header: "Database",
      mono: true,
      cell: (row) => (
        <Link
          to="/databases/$engine/$name"
          params={{ engine: row.engine, name: row.name }}
          className="-mx-1 rounded-[4px] px-1 py-0.5 font-medium text-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
        >
          {row.name}
        </Link>
      ),
      sortValue: (row) => row.name,
    },
    {
      id: "owner",
      header: "Owner",
      mono: true,
      hideBelow: "md",
      cell: (row) => (row.owner ? <span className="text-fg-muted">{row.owner}</span> : <span className="text-fg-faint">-</span>),
      sortValue: (row) => row.owner ?? null,
    },
    {
      id: "tables",
      header: "Tables",
      align: "end",
      mono: true,
      hideBelow: "sm",
      cell: (row) => <span className="text-fg-muted">{formatCount(row.tables)}</span>,
      sortValue: (row) => row.tables,
    },
    {
      id: "size",
      header: "Size",
      align: "end",
      mono: true,
      width: "w-28",
      cell: (row) => (row.size ? <span className="text-fg-muted">{row.size}</span> : <span className="text-fg-faint">-</span>),
      sortValue: (row) => row.size ?? null,
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={databases}
      getRowId={(row) => `${row.engine}/${row.name}`}
      caption={caption}
      loading={loading}
      {...(empty !== undefined ? { empty } : {})}
      rowActions={(row) => <RowActions database={row} />}
      defaultSort={{ column: "name", direction: "ascending" }}
      {...(className !== undefined ? { className } : {})}
    />
  );
}

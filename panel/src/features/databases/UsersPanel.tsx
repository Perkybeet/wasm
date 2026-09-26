import { useQuery } from "@tanstack/react-query";
import { MoreHorizontal, Plus, ShieldMinus, ShieldPlus, Trash2, UserRound } from "lucide-react";
import { useState } from "react";

import type { DatabaseUser, Engine } from "../../api/queries/databases";
import { databaseUsersQuery } from "../../api/queries/databases";
import { QueryState } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { Select } from "../../components/ui/Select";
import { CreateUserDialog } from "./CreateUserDialog";
import { engineLabel } from "./data";
import { GrantDialog } from "./GrantDialog";
import type { GrantMode } from "./GrantDialog";
import { useDatabaseActions } from "./useDatabaseActions";

function UserActions({ user }: { user: DatabaseUser }) {
  const { deleteUser } = useDatabaseActions();
  const [grantMode, setGrantMode] = useState<GrantMode | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  return (
    <>
      <Menu align="end" trigger={<IconButton label={`Actions for ${user.username}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
        <MenuItem icon={<ShieldPlus />} onClick={() => setGrantMode("grant")}>
          Grant privileges
        </MenuItem>
        <MenuItem icon={<ShieldMinus />} onClick={() => setGrantMode("revoke")}>
          Revoke privileges
        </MenuItem>
        <MenuItem icon={<Trash2 />} destructive onClick={() => setConfirmOpen(true)}>
          Delete user
        </MenuItem>
      </Menu>
      <GrantDialog
        mode={grantMode ?? "grant"}
        user={user}
        open={grantMode !== null}
        onOpenChange={(next) => {
          if (!next) setGrantMode(null);
        }}
      />
      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Delete ${user.username}`}
        description={`Removes '${user.username}' from ${engineLabel(user.engine)}. Databases it owns are not deleted, but it can no longer connect.`}
        confirmText={user.username}
        actionLabel="Delete user"
        onConfirm={async () => {
          await deleteUser.mutateAsync({ engine: user.engine, username: user.username, host: user.host });
        }}
      />
    </>
  );
}

export interface UsersPanelProps {
  engines: readonly Engine[];
  /** The engines are still loading: which of them run is not known yet. */
  loading?: boolean;
  engine: string;
  onEngineChange: (engine: string) => void;
}

/** The users of one engine, with grant, revoke and delete - engine-scoped, like the CLI's `wasm db user` commands. */
export function UsersPanel({ engines, loading = false, engine, onEngineChange }: UsersPanelProps) {
  const users = useQuery({ ...databaseUsersQuery(engine), enabled: engine !== "" });
  const runnable = engines.filter((item) => item.installed && item.running);

  const columns: Column<DatabaseUser>[] = [
    { id: "username", header: "Username", mono: true, cell: (row) => row.username, sortValue: (row) => row.username },
    { id: "host", header: "Host", mono: true, width: "w-40", hideBelow: "sm", cell: (row) => row.host, sortValue: (row) => row.host },
    {
      id: "privileges",
      header: "Privileges",
      cell: (row) =>
        (row.privileges ?? []).length === 0 ? (
          <span className="text-fg-faint">Default</span>
        ) : (
          <span className="flex flex-wrap gap-1">
            {(row.privileges ?? []).map((privilege) => (
              <Badge key={privilege} mono>
                {privilege}
              </Badge>
            ))}
          </span>
        ),
    },
  ];

  return (
    <Section
      title="Users"
      description="Logins on one engine. A user is granted access to a database, not created inside one."
      actions={
        <>
          <Select
            aria-label="Engine"
            size="sm"
            value={engine}
            onValueChange={onEngineChange}
            options={runnable.map((item) => ({ value: item.name, label: engineLabel(item.name) }))}
            disabled={runnable.length === 0}
          />
          <CreateUserDialog
            engines={runnable}
            trigger={
              <Button size="sm" icon={<Plus aria-hidden="true" />} disabled={runnable.length === 0}>
                New user
              </Button>
            }
          />
        </>
      }
    >
      {loading ? (
        // The table's own placeholder: until the engines answer, "No running engine" would be
        // a guess, and a card of that size swapped for a table moved everything around it.
        <div aria-busy="true">
          <span className="sr-only">Loading users</span>
          <DataTable columns={columns} rows={[]} getRowId={() => ""} caption="Database users" loading />
        </div>
      ) : runnable.length === 0 ? (
        <EmptyState
          level={3}
          icon={<UserRound />}
          title="No running engine"
          description="Start a database engine above to manage its users."
        />
      ) : (
        <QueryState
          query={users}
          label="users"
          skeleton={<DataTable columns={columns} rows={[]} getRowId={() => ""} caption={`Users on ${engineLabel(engine)}`} loading />}
          isEmpty={(data) => data.users.length === 0}
          empty={
            <EmptyState
              icon={<UserRound />}
              title="No users on this engine"
              description={`Create a login to connect applications or people to ${engineLabel(engine)}.`}
            />
          }
        >
          {(data) => (
            <DataTable
              columns={columns}
              rows={data.users}
              getRowId={(row) => `${row.username}@${row.host}`}
              caption={`Users on ${engineLabel(engine)}`}
              rowActions={(row) => <UserActions user={row} />}
              defaultSort={{ column: "username", direction: "ascending" }}
            />
          )}
        </QueryState>
      )}
    </Section>
  );
}

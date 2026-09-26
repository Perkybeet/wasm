import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { MoreHorizontal, Trash2 } from "lucide-react";
import { useState } from "react";

import { databaseQuery } from "../../api/queries/databases";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { KeyValueList, KeyValueListSkeleton } from "../../components/page/KeyValueList";
import type { KeyValueItem } from "../../components/page/KeyValueList";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { ConnectionString } from "./ConnectionString";
import { DatabaseBackups } from "./DatabaseBackups";
import { engineLabel } from "./data";
import { SqlConsole } from "./SqlConsole";
import { useDatabaseActions } from "./useDatabaseActions";

function Overview({ engine, name }: { engine: string; name: string }) {
  const database = useQuery(databaseQuery(engine, name));

  if (database.isError && database.data === undefined) {
    return <ErrorBlock error={database.error} title="Could not load this database" onRetry={() => void database.refetch()} retrying={database.isRefetching} />;
  }
  if (database.data === undefined) {
    return (
      <div className="rounded-card border border-border bg-surface px-4 py-1">
        <KeyValueListSkeleton rows={4} />
      </div>
    );
  }

  const info = database.data;
  const items: KeyValueItem[] = [
    { label: "Engine", value: engineLabel(info.engine) },
    { label: info.engine === "redis" ? "Keys" : "Tables", value: info.tables, mono: true },
    { label: "Size", value: info.size, mono: true },
    { label: "Owner", value: info.owner, mono: true },
    { label: "Encoding", value: info.encoding, mono: true },
  ];

  return (
    <div className="rounded-card border border-border bg-surface px-4 py-1">
      <KeyValueList items={items} />
    </div>
  );
}

/** One database: what it is, its own backups, a connection string on request, and the console. */
export function DatabasePage({ engine, name }: { engine: string; name: string }) {
  const navigate = useNavigate();
  const { dropDatabase } = useDatabaseActions();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const keysLabel = engine === "redis" ? "slot" : "database";

  return (
    <>
      <PageHeader
        title={name}
        description={`A ${engineLabel(engine)} ${keysLabel} on this machine.`}
        breadcrumbs={[{ label: "Databases", to: "/databases" }]}
        actions={
          <Menu align="end" trigger={<Button icon={<MoreHorizontal aria-hidden="true" />}>Actions</Button>}>
            <MenuItem icon={<Trash2 />} destructive onClick={() => setConfirmOpen(true)}>
              Drop {keysLabel}
            </MenuItem>
          </Menu>
        }
      />
      <div className="flex flex-col gap-8">
        <Section title="Overview">
          <Overview engine={engine} name={name} />
          <CommandHint command={`wasm db info ${name} --engine ${engine}`} label="From a terminal" />
        </Section>

        {/* The console is what this page is opened for most: right under what the database is. */}
        <SqlConsole engine={engine} database={name} />

        <DatabaseBackups engine={engine} database={name} />

        <ConnectionString engine={engine} database={name} />
      </div>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title={`Drop ${name}`}
        description={`This permanently deletes '${name}' on ${engineLabel(engine)} and everything in it. This cannot be undone.`}
        confirmText={name}
        actionLabel={`Drop ${keysLabel}`}
        onConfirm={async () => {
          await dropDatabase.mutateAsync({ engine, name });
          void navigate({ to: "/databases" });
        }}
      />
    </>
  );
}

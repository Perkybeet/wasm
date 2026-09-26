import { useQuery } from "@tanstack/react-query";
import { Database, Plus } from "lucide-react";
import { useMemo, useState } from "react";

import { databasesQuery, enginesQuery } from "../../api/queries/databases";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { QueryState } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { Select } from "../../components/ui/Select";
import { CreateDatabaseDialog } from "./CreateDatabaseDialog";
import { DatabasesTable } from "./DatabasesTable";
import { engineLabel } from "./data";
import { EnginesStrip } from "./EnginesStrip";
import { UsersPanel } from "./UsersPanel";

const ALL = "all";

/** Databases, users and engines across the machine (D8: engines strip, then tables, not cards). */
export function DatabasesPage() {
  const engines = useQuery(enginesQuery());
  const [filter, setFilter] = useState<string>(ALL);
  const databases = useQuery(databasesQuery(filter === ALL ? null : filter));

  const runnable = useMemo(() => engines.data?.engines.filter((item) => item.installed && item.running) ?? [], [engines.data]);
  const [usersEngine, setUsersEngine] = useState<string | null>(null);
  const activeUsersEngine = usersEngine ?? runnable[0]?.name ?? "";

  return (
    <>
      <PageHeader title="Databases" description="Database engines on this machine, their databases, users and backups." />
      <div className="flex flex-col gap-8">
        <EnginesStrip />

        <Section
          title="Databases"
          actions={
            <>
              <Select
                aria-label="Filter by engine"
                size="sm"
                value={filter}
                onValueChange={setFilter}
                options={[{ value: ALL, label: "Every engine" }, ...(engines.data?.engines ?? []).map((item) => ({ value: item.name, label: engineLabel(item.name) }))]}
              />
              <CreateDatabaseDialog
                engines={runnable}
                trigger={
                  <Button size="sm" variant="primary" icon={<Plus aria-hidden="true" />} disabled={runnable.length === 0}>
                    New database
                  </Button>
                }
              />
            </>
          }
        >
          <QueryState
            query={databases}
            label="databases"
            // The table itself with placeholder rows, and the hint under it: the loaded shape.
            skeleton={
              <div className="flex flex-col gap-3">
                <DatabasesTable databases={[]} caption="Databases" loading />
                <CommandHint command="wasm db list" label="From a terminal" />
              </div>
            }
            isEmpty={(data) => data.databases.length === 0}
            empty={
              <EmptyState
                icon={<Database />}
                title="No databases yet"
                description="A database holds an application's data. Create one on a running engine, or point an app at an existing one."
                action={
                  <CreateDatabaseDialog
                    engines={runnable}
                    trigger={
                      <Button variant="primary" icon={<Plus aria-hidden="true" />} disabled={runnable.length === 0}>
                        New database
                      </Button>
                    }
                  />
                }
                command="wasm db create"
              />
            }
          >
            {(data) => (
              <div className="flex flex-col gap-3">
                <DatabasesTable databases={data.databases} caption={filter === ALL ? "Databases" : `Databases on ${engineLabel(filter)}`} />
                <CommandHint command="wasm db list" label="From a terminal" />
              </div>
            )}
          </QueryState>
        </Section>

        <UsersPanel
          engines={engines.data?.engines ?? []}
          loading={engines.isPending}
          engine={activeUsersEngine}
          onEngineChange={setUsersEngine}
        />
      </div>
    </>
  );
}

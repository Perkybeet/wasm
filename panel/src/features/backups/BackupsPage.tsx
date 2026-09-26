import { useQuery } from "@tanstack/react-query";
import { Archive, Plus, X } from "lucide-react";
import { useMemo } from "react";

import { backupsQuery } from "../../api/queries/backups";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { EmptyState } from "../../components/ui/EmptyState";
import { Select } from "../../components/ui/Select";
import { BackupsTable } from "./BackupsTable";
import { CreateBackupDialog } from "./CreateBackupDialog";
import { backupDomains, filterBackups, isFiltered } from "./filters";
import type { BackupsSearch } from "./filters";
import { SchedulesSection } from "./SchedulesSection";
import { StorageUsageBar } from "./StorageUsageBar";
import { useBackupRefresh } from "./useBackupRefresh";

const ALL = "all";

type SearchPatch = { [K in keyof BackupsSearch]?: BackupsSearch[K] | undefined };

export interface BackupsPageProps {
  search: BackupsSearch;
  onSearchChange: (search: BackupsSearch, options?: { replace?: boolean }) => void;
}

/** Every backup on the machine, its storage footprint, and the schedules that create more of them. */
export function BackupsPage({ search, onSearchChange }: BackupsPageProps) {
  useBackupRefresh();
  const backups = useQuery(backupsQuery(search.domain ?? null));
  const all = useMemo(() => backups.data?.backups ?? [], [backups.data]);
  const shown = useMemo(() => filterBackups(all, search), [all, search]);
  const domains = useMemo(() => backupDomains(all), [all]);
  const filtered = isFiltered(search);

  const set = (patch: SearchPatch): void => {
    const next: SearchPatch = { ...search, ...patch };
    const clean: BackupsSearch = {};
    if (next.domain) clean.domain = next.domain;
    if (next.database) clean.database = true;
    onSearchChange(clean);
  };

  return (
    <>
      <PageHeader
        title="Backups"
        description="Snapshots of your applications, their schedules and the storage they use."
        actions={
          <CreateBackupDialog
            trigger={
              <Button variant="primary" icon={<Plus aria-hidden="true" />}>
                New backup
              </Button>
            }
          />
        }
      />
      <div className="flex flex-col gap-8">
        <StorageUsageBar />

        <Section title="Backups">
          {backups.isError && backups.data === undefined ? (
            <ErrorBlock error={backups.error} title="Could not load backups" onRetry={() => void backups.refetch()} retrying={backups.isRefetching} />
          ) : backups.data !== undefined && all.length === 0 ? (
            <EmptyState
              icon={<Archive />}
              title="No backups yet"
              description="A backup is a snapshot of an application's files - and, if you ask for it, its databases too."
              action={
                <CreateBackupDialog
                  trigger={
                    <Button variant="primary" icon={<Plus aria-hidden="true" />}>
                      New backup
                    </Button>
                  }
                />
              }
              command="wasm backup create <domain>"
              className="py-16"
            />
          ) : (
            <div className="flex flex-col gap-4">
              <div role="search" aria-label="Filter backups" className="flex flex-wrap items-center gap-2">
                <Select
                  aria-label="Application"
                  size="sm"
                  value={search.domain ?? ALL}
                  onValueChange={(value) => set({ domain: value === ALL ? undefined : value })}
                  options={[{ value: ALL, label: "Every application" }, ...domains.map((domain) => ({ value: domain, label: domain }))]}
                />
                <Checkbox
                  checked={search.database === true}
                  onCheckedChange={(checked) => set({ database: checked ? true : undefined })}
                  label="Includes a database"
                />
                {filtered ? (
                  <Button size="sm" variant="ghost" icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
                    Clear filters
                  </Button>
                ) : null}
              </div>
              <BackupsTable
                backups={shown}
                caption={filtered ? "Backups matching the filters" : "Every backup"}
                loading={backups.isPending}
                empty={
                  <EmptyState
                    title="No backup matches"
                    description="Nothing matches these filters."
                    action={
                      <Button icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
                        Clear filters
                      </Button>
                    }
                    className="border-0 py-8"
                  />
                }
              />
              <CommandHint command="wasm backup list" label="From a terminal" />
            </div>
          )}
        </Section>

        <SchedulesSection />
      </div>
    </>
  );
}

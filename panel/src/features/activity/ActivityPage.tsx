import { useQuery } from "@tanstack/react-query";
import { History, Search, X } from "lucide-react";
import { useMemo, useState } from "react";

import { jobsQuery } from "../../api/queries/jobs";
import { PageHeader } from "../../app/PageHeader";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { ActivityTable } from "./ActivityTable";
import { JobLogDrawer } from "./JobLogDrawer";
import { actionLabel, filterJobs, isFiltered } from "./data";
import type { ActivityJob, ActivitySearch } from "./data";

const ALL = "all";
const STATUSES = ["pending", "running", "completed", "failed", "cancelled"] as const;
const TYPES = ["deploy", "update", "backup", "restore", "cert_create", "cert_renew", "service_action", "site_action", "delete", "custom"] as const;

export interface ActivityPageProps {
  search: ActivitySearch;
  onSearchChange: (search: ActivitySearch, options?: { replace?: boolean }) => void;
}

/** A change to the filters: a key set to undefined is cleared. */
type SearchPatch = { [K in keyof ActivitySearch]?: ActivitySearch[K] | undefined };

/**
 * Every job WASM has queued, merged into one timeline. There is no audit log endpoint to merge
 * in alongside it (see `./data`), and a job carries no actor, so neither appears here - both
 * are reported as gaps rather than invented.
 */
export function ActivityPage({ search, onSearchChange }: ActivityPageProps) {
  const jobs = useQuery(jobsQuery({ limit: 100, ...(search.status ? { status: search.status } : {}), ...(search.domain ? { domain: search.domain } : {}) }));
  const [openJob, setOpenJob] = useState<ActivityJob | null>(null);

  const all = useMemo(() => jobs.data?.jobs ?? [], [jobs.data]);
  const shown = useMemo(() => filterJobs(all, search), [all, search]);
  const filtered = isFiltered(search);

  const set = (patch: SearchPatch): void => {
    const next: SearchPatch = { ...search, ...patch };
    const clean: ActivitySearch = {};
    if (next.status) clean.status = next.status;
    if (next.type) clean.type = next.type;
    if (next.domain) clean.domain = next.domain;
    onSearchChange(clean, { replace: true });
  };

  return (
    <>
      <PageHeader
        title="Activity"
        description="Jobs WASM has run on this machine: deploys, updates, backups, certificates, service and site actions."
      />
      <p className="-mt-4 mb-6 max-w-[68ch] text-13 text-pretty text-fg-muted">
        This is jobs history, not a full audit log: there is no endpoint yet for sign-ins or configuration changes,
        and a job does not record who queued it, only what it did and how it ended.
      </p>

      {jobs.isError && jobs.data === undefined ? (
        <ErrorBlock error={jobs.error} title="Could not load activity" onRetry={() => void jobs.refetch()} retrying={jobs.isRefetching} />
      ) : jobs.data !== undefined && all.length === 0 && !filtered ? (
        <EmptyState
          level={2}
          icon={<History />}
          title="Nothing has run yet"
          description="Deploys, updates, backups and other jobs will appear here as they happen."
          command="wasm jobs list"
          className="py-16"
        />
      ) : (
        <div className="flex flex-col gap-4">
          <div role="search" aria-label="Filter activity" className="flex flex-wrap items-end gap-2">
            <Input
              type="search"
              aria-label="Filter by domain"
              placeholder="Domain"
              icon={<Search />}
              value={search.domain ?? ""}
              onValueChange={(value: string) => set({ domain: value === "" ? undefined : value })}
              className="w-full sm:w-56"
              autoComplete="off"
              spellCheck={false}
            />
            <Select
              aria-label="Result"
              value={search.status ?? ALL}
              onValueChange={(value) => set({ status: value === ALL ? undefined : value })}
              options={[{ value: ALL, label: "Every result" }, ...STATUSES.map((status) => ({ value: status, label: status }))]}
              className="min-w-36"
            />
            <Select
              aria-label="Action"
              value={search.type ?? ALL}
              onValueChange={(value) => set({ type: value === ALL ? undefined : value })}
              options={[{ value: ALL, label: "Every action" }, ...TYPES.map((type) => ({ value: type, label: actionLabel(type) }))]}
              className="min-w-40"
            />
            {filtered ? (
              <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
                Clear filters
              </Button>
            ) : null}
          </div>

          <ActivityTable
            jobs={shown}
            caption={filtered ? "Activity matching the filters" : "Activity"}
            loading={jobs.isPending}
            onRowActivate={setOpenJob}
            empty={
              <EmptyState
                title="No job matches"
                description="Nothing on this machine matches these filters."
                action={
                  <Button icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
                    Clear filters
                  </Button>
                }
                className="border-0 py-8"
              />
            }
          />
        </div>
      )}

      <JobLogDrawer job={openJob} onOpenChange={(open) => !open && setOpenJob(null)} />
    </>
  );
}

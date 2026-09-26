import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { History, User, X } from "lucide-react";
import { useMemo, useState } from "react";

import { isApiError } from "../../api/client";
import { auditPagesQuery } from "../../api/queries/audit";
import { jobsQuery } from "../../api/queries/jobs";
import { PageHeader } from "../../app/PageHeader";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { ActivityTable } from "./ActivityTable";
import { JobLogDrawer } from "./JobLogDrawer";
import { AUDIT_RESULTS, JOB_STATUSES, isFiltered, mergeActivity, resultOptions, resultValidFor } from "./data";
import type { ActivityJob, ActivitySearch } from "./data";

const ALL = "all";
/** How many more jobs a "Load more" click asks for; the jobs endpoint has no cursor, only a limit. */
const JOBS_PAGE = 50;
/** Entries per page of the audit log's own keyset cursor. */
const AUDIT_PAGE = 30;

export interface ActivityPageProps {
  search: ActivitySearch;
  onSearchChange: (search: ActivitySearch, options?: { replace?: boolean }) => void;
}

/** A change to the filters: a key set to undefined is cleared. */
type SearchPatch = { [K in keyof ActivitySearch]?: ActivitySearch[K] | undefined };

/**
 * Every job WASM has run and every action the audit log recorded, merged into one filterable,
 * newest-first timeline. A job row opens its captured log; an audit row has none. A session
 * without the `admin` scope gets 403 from the audit log - this shows jobs only then, with a
 * quiet note instead of an error, since a read-scoped operator did nothing wrong.
 */
export function ActivityPage({ search, onSearchChange }: ActivityPageProps) {
  const [jobsLimit, setJobsLimit] = useState(JOBS_PAGE);
  const [openJob, setOpenJob] = useState<ActivityJob | null>(null);

  const wantsJobs = search.kind !== "audit";
  const wantsAudit = search.kind !== "jobs";
  const resultAppliesToJobs = search.result === undefined || JOB_STATUSES.has(search.result);
  const resultAppliesToAudit = search.result === undefined || AUDIT_RESULTS.has(search.result);
  // A result picked for the other vocabulary (e.g. "denied" while kind is "jobs") matches
  // nothing there; excluding the source outright is clearer than fetching it unfiltered.
  const includeJobsQuery = wantsJobs && resultAppliesToJobs;
  const includeAuditQuery = wantsAudit && resultAppliesToAudit;
  const jobStatus = includeJobsQuery && search.result !== undefined ? search.result : undefined;
  const auditResultFilter = includeAuditQuery && search.result !== undefined ? search.result : undefined;

  // A filter that changes what jobs mean starts "Load more" over; the audit log's own
  // infinite query already restarts on a query-key change, jobs' flat limit does not. Reset
  // during render (React's documented way to react to a prop/derived-value change) rather than
  // in an effect, so it takes effect before the query below fires with the old limit.
  const [resetFor, setResetFor] = useState(jobStatus);
  if (resetFor !== jobStatus) {
    setResetFor(jobStatus);
    setJobsLimit(JOBS_PAGE);
  }

  const jobs = useQuery({
    ...jobsQuery({ limit: jobsLimit, ...(jobStatus !== undefined ? { status: jobStatus } : {}) }),
    enabled: includeJobsQuery,
  });
  const audit = useInfiniteQuery({
    ...auditPagesQuery({ limit: AUDIT_PAGE, ...(auditResultFilter !== undefined ? { result: auditResultFilter } : {}) }),
    enabled: includeAuditQuery,
  });

  const auditForbidden = includeAuditQuery && audit.isError && isApiError(audit.error) && audit.error.status === 403;
  const includeAudit = includeAuditQuery && !auditForbidden;

  const allJobs = useMemo(() => jobs.data?.jobs ?? [], [jobs.data]);
  const jobsComplete = !includeJobsQuery || (jobs.data !== undefined && allJobs.length >= jobs.data.total);

  const allEntries = useMemo(() => audit.data?.pages.flatMap((page) => page.items) ?? [], [audit.data]);
  const auditComplete = !includeAudit || (audit.data !== undefined && (audit.data.pages.at(-1)?.next_before ?? null) === null);

  const { rows, hasMore } = useMemo(
    () =>
      mergeActivity({
        jobs: includeJobsQuery ? allJobs : [],
        jobsComplete,
        entries: includeAudit ? allEntries : [],
        auditComplete,
        actor: search.actor,
      }),
    [includeJobsQuery, allJobs, jobsComplete, includeAudit, allEntries, auditComplete, search.actor],
  );

  const filtered = isFiltered(search);
  const loading = (includeJobsQuery && jobs.isPending) || (includeAuditQuery && audit.isPending);
  const jobsHardError = includeJobsQuery && jobs.isError && jobs.data === undefined;
  const auditHardError = includeAuditQuery && !auditForbidden && audit.isError && audit.data === undefined;
  const jobsLoaded = !includeJobsQuery || jobs.data !== undefined;
  const auditLoaded = !includeAuditQuery || auditForbidden || audit.data !== undefined;

  const set = (patch: SearchPatch): void => {
    const next: SearchPatch = { ...search, ...patch };
    const clean: ActivitySearch = {};
    if (next.kind) clean.kind = next.kind;
    if (next.result !== undefined && resultValidFor(next.result, clean.kind)) clean.result = next.result;
    if (next.actor) clean.actor = next.actor;
    onSearchChange(clean, { replace: true });
  };

  const loadMore = (): void => {
    if (includeJobsQuery && !jobsComplete) setJobsLimit((limit) => limit + JOBS_PAGE);
    if (includeAudit && !auditComplete) void audit.fetchNextPage();
  };

  if (jobsHardError || auditHardError) {
    const failing = jobsHardError ? jobs : audit;
    return (
      <>
        <PageHeader
          title="Activity"
          description="Every job and audited action on this machine, merged into one timeline, newest first."
        />
        <ErrorBlock
          error={failing.error}
          title="Could not load activity"
          onRetry={() => {
            if (jobsHardError) void jobs.refetch();
            if (auditHardError) void audit.refetch();
          }}
          retrying={(jobsHardError && jobs.isRefetching) || (auditHardError && audit.isRefetching)}
        />
      </>
    );
  }

  const nothingYet = jobsLoaded && auditLoaded && !filtered && rows.length === 0;

  return (
    <>
      <PageHeader
        title="Activity"
        description="Every job and audited action on this machine, merged into one timeline, newest first."
      />
      {wantsAudit && auditForbidden ? (
        <p role="status" className="-mt-4 mb-6 max-w-[68ch] text-13 text-pretty text-fg-muted">
          The audit log needs an admin token. Showing jobs only.
        </p>
      ) : null}

      {nothingYet ? (
        <EmptyState
          level={2}
          icon={<History />}
          title="Nothing has run yet"
          description="Deploys, updates, backups and audited actions will appear here as they happen."
          command="wasm jobs list"
          className="py-16"
        />
      ) : (
        <div className="flex flex-col gap-4">
          <div role="search" aria-label="Filter activity" className="flex flex-wrap items-end gap-2">
            <Select
              aria-label="Kind"
              value={search.kind ?? ALL}
              onValueChange={(value) => set({ kind: value === ALL ? undefined : value })}
              options={[
                { value: ALL, label: "Everything" },
                { value: "jobs", label: "Jobs" },
                { value: "audit", label: "Audited actions" },
              ]}
              className="min-w-36"
            />
            <Select
              aria-label="Result"
              value={search.result ?? ALL}
              onValueChange={(value) => set({ result: value === ALL ? undefined : value })}
              options={[{ value: ALL, label: "Every result" }, ...resultOptions(search.kind)]}
              className="min-w-44"
            />
            <Input
              type="search"
              aria-label="Actor"
              placeholder="Actor"
              icon={<User />}
              value={search.actor ?? ""}
              onValueChange={(value) => set({ actor: value === "" ? undefined : value })}
              className="w-full sm:w-56"
              autoComplete="off"
              spellCheck={false}
            />
            {filtered ? (
              <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
                Clear filters
              </Button>
            ) : null}
          </div>

          <ActivityTable
            rows={rows}
            caption={filtered ? "Activity matching the filters" : "Activity"}
            loading={loading}
            onOpenJobLog={setOpenJob}
            empty={
              <EmptyState
                title="No activity matches"
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

          {hasMore ? (
            <div>
              <Button
                size="sm"
                loading={(includeJobsQuery && !jobsComplete && jobs.isFetching) || (includeAudit && audit.isFetchingNextPage)}
                onClick={loadMore}
              >
                Load more
              </Button>
            </div>
          ) : null}
        </div>
      )}

      <JobLogDrawer job={openJob} onOpenChange={(open) => !open && setOpenJob(null)} />
    </>
  );
}

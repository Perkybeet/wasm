import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Boxes, Plus, Search, X } from "lucide-react";
import { useMemo } from "react";

import { appsQuery } from "../../api/queries/apps";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { STATUS } from "../../components/ui/StatusPill";
import { Button, buttonClassName } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { Input } from "../../components/ui/Input";
import { Kbd } from "../../components/ui/Kbd";
import { Select } from "../../components/ui/Select";
import { AppRowActions } from "./AppRowActions";
import { AppsTable } from "./AppsTable";
import { latestDeployByDomain, recentDeploysQuery, useLatestMetrics } from "./data";
import { STATE_FILTERS, appTypes, filterApps, isFiltered } from "./filters";
import type { AppsSearch } from "./filters";
import { useStateTransitions } from "./useStateTransitions";

const ALL = "all";

/** A change to the filters: a key set to undefined is cleared. */
type SearchPatch = { [K in keyof AppsSearch]?: AppsSearch[K] | undefined };

export interface AppsPageProps {
  search: AppsSearch;
  /**
   * Sets the filters in the URL. `replace` for typing, so the history does not gain an entry
   * per keystroke; choosing a filter pushes one, so Back undoes it.
   */
  onSearchChange: (search: AppsSearch, options?: { replace?: boolean }) => void;
}

function NewAppLink() {
  return (
    <Link to="/apps/new" className={buttonClassName("primary")}>
      <Plus aria-hidden="true" />
      New application
    </Link>
  );
}

/**
 * Every application on the machine in one table: searchable with `/`, filtered by state and
 * type through the URL, each row opening its app or acting on it from its menu.
 */
export function AppsPage({ search, onSearchChange }: AppsPageProps) {
  const apps = useQuery(appsQuery());
  const deploys = useQuery(recentDeploysQuery());
  const metrics = useLatestMetrics();

  const all = useMemo(() => apps.data?.apps ?? [], [apps.data]);
  const shown = useMemo(() => filterApps(all, search), [all, search]);
  const latest = useMemo(() => latestDeployByDomain(deploys.data?.items ?? []), [deploys.data]);
  const types = useMemo(() => appTypes(all), [all]);
  useStateTransitions(apps.data?.apps);

  const set = (patch: SearchPatch, replace = false): void => {
    const next: SearchPatch = { ...search, ...patch };
    const clean: AppsSearch = {};
    if (next.q) clean.q = next.q;
    if (next.state) clean.state = next.state;
    if (next.type) clean.type = next.type;
    onSearchChange(clean, { replace });
  };

  const filtered = isFiltered(search);
  const count = apps.data ? (filtered ? `${String(shown.length)} of ${String(all.length)}` : String(all.length)) : null;

  return (
    <>
      <PageHeader
        title="Applications"
        description="Every app deployed on this machine, its state and its last deploy."
        actions={<NewAppLink />}
      />

      {apps.isError && apps.data === undefined ? (
        <ErrorBlock
          error={apps.error}
          title="Could not load applications"
          onRetry={() => void apps.refetch()}
          retrying={apps.isRefetching}
        />
      ) : apps.data !== undefined && all.length === 0 ? (
        <EmptyState
          level={2}
          icon={<Boxes />}
          title="Deploy your first application"
          description="Point WASM at a Git repository or a directory. It detects the stack, builds it, gives it a systemd unit, a site and a certificate."
          action={<NewAppLink />}
          command="wasm create -d example.com -s https://github.com/you/app"
          className="py-16"
        />
      ) : (
        <div className="flex flex-col gap-4">
          <div role="search" aria-label="Filter applications" className="flex flex-wrap items-end gap-2">
            <Input
              type="search"
              aria-label="Search applications"
              placeholder="Search by domain or type"
              data-page-search=""
              value={search.q ?? ""}
              onValueChange={(value: string) => set({ q: value }, true)}
              icon={<Search />}
              // The shortcut is for keyboards; a phone has no use for the hint.
              suffix={search.q ? undefined : <Kbd className="pointer-coarse:hidden">/</Kbd>}
              className="w-full sm:w-72"
              autoComplete="off"
              spellCheck={false}
            />
            <Select
              aria-label="State"
              value={search.state ?? ALL}
              onValueChange={(value) => set({ state: STATE_FILTERS.find((state) => state === value) })}
              options={[
                { value: ALL, label: "Every state" },
                ...STATE_FILTERS.map((state) => ({ value: state, label: STATUS[state].label })),
              ]}
              className="min-w-36"
            />
            <Select
              aria-label="Type"
              value={search.type ?? ALL}
              onValueChange={(value) => set({ type: value === ALL ? undefined : value })}
              options={[{ value: ALL, label: "Every type" }, ...types.map((type) => ({ value: type, label: type }))]}
              className="min-w-36"
            />
            {filtered ? (
              <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
                Clear filters
              </Button>
            ) : null}
            <p role="status" className="ml-auto self-center text-13 text-fg-muted">
              {count === null ? "" : `${count} ${all.length === 1 && !filtered ? "application" : "applications"}`}
            </p>
          </div>

          <AppsTable
            apps={shown}
            deploys={latest}
            metrics={metrics}
            caption={filtered ? "Applications matching the filters" : "Applications"}
            loading={apps.isPending}
            detail="full"
            rowActions={(app) => <AppRowActions app={app} />}
            empty={
              <EmptyState
                title="No application matches"
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
          {/* Drawn with the rows, not before: under a list of unknown length it would only be
              pushed down the page when they arrive. */}
          {apps.isPending ? null : <CommandHint command="wasm list" label="From a terminal" />}
        </div>
      )}
    </>
  );
}

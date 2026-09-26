import { useQuery } from "@tanstack/react-query";
import { Cog, Plus, Search, X } from "lucide-react";
import { useMemo, useState } from "react";

import { servicesQuery } from "../../api/queries/services";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { EmptyState } from "../../components/ui/EmptyState";
import { Input } from "../../components/ui/Input";
import { Kbd } from "../../components/ui/Kbd";
import { Switch } from "../../components/ui/Switch";
import { CreateServiceDialog } from "./CreateServiceDialog";
import { ServiceRowActions } from "./ServiceRowActions";
import { ServicesTable } from "./ServicesTable";
import { filterServices, isFiltered } from "./data";
import type { ServicesSearch } from "./data";

/** A change to the filters: a key set to undefined is cleared. */
type SearchPatch = { [K in keyof ServicesSearch]?: ServicesSearch[K] | undefined };

export interface ServicesPageProps {
  search: ServicesSearch;
  onSearchChange: (search: ServicesSearch, options?: { replace?: boolean }) => void;
}

/**
 * Every systemd unit WASM created on this machine, searchable and acted on from its own row.
 * "Show all units" widens the request to `wasm_only=false`: a unit another package created
 * appears too, marked "Foreign" and read-only (see `ServiceRowActions`).
 */
export function ServicesPage({ search, onSearchChange }: ServicesPageProps) {
  const showAll = search.all === true;
  const services = useQuery(servicesQuery(!showAll));
  const [createOpen, setCreateOpen] = useState(false);

  const fetched = useMemo(() => services.data?.services ?? [], [services.data]);
  const shown = useMemo(() => filterServices(fetched, search), [fetched, search]);

  const set = (patch: SearchPatch, replace = false): void => {
    const next: SearchPatch = { ...search, ...patch };
    const clean: ServicesSearch = {};
    if (next.q) clean.q = next.q;
    if (next.all) clean.all = true;
    onSearchChange(clean, { replace });
  };

  // "Clear filters" only ever clears the text search: "Show all units" is a scope, not a
  // filter on what came back, so clearing one leaves the other as the operator set it.
  const clearTextFilter = (): void => onSearchChange(showAll ? { all: true } : {});

  const filtered = isFiltered(search);
  const count = services.data
    ? filtered
      ? `${String(shown.length)} of ${String(fetched.length)}`
      : String(fetched.length)
    : null;

  return (
    <>
      <PageHeader
        title="Services"
        description={'Every systemd unit WASM manages on this machine. Turn on "Show all units" to see what other packages created too, read-only.'}
        actions={
          <Button variant="primary" icon={<Plus aria-hidden="true" />} onClick={() => setCreateOpen(true)}>
            New service
          </Button>
        }
      />

      {services.isError && services.data === undefined ? (
        <ErrorBlock
          error={services.error}
          title="Could not load services"
          onRetry={() => void services.refetch()}
          retrying={services.isRefetching}
        />
      ) : services.data !== undefined && fetched.length === 0 ? (
        <EmptyState
          level={2}
          icon={<Cog />}
          title="Create your first service"
          description="A systemd unit run under this machine's service user, restarted automatically and started at boot."
          action={
            <Button variant="primary" icon={<Plus aria-hidden="true" />} onClick={() => setCreateOpen(true)}>
              New service
            </Button>
          }
          command="wasm service create --name worker --command '/usr/bin/node worker.js' --directory /var/www/worker"
          className="py-16"
        />
      ) : (
        <div className="flex flex-col gap-4">
          <div role="search" aria-label="Filter services" className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <Input
              type="search"
              aria-label="Search services"
              placeholder="Search by unit name or command"
              data-page-search=""
              value={search.q ?? ""}
              onValueChange={(value: string) => set({ q: value }, true)}
              icon={<Search />}
              suffix={search.q ? undefined : <Kbd className="pointer-coarse:hidden">/</Kbd>}
              className="w-full sm:w-80"
              autoComplete="off"
              spellCheck={false}
            />
            <Switch
              label="Show all units"
              checked={showAll}
              onCheckedChange={(checked) => set({ all: checked ? true : undefined }, true)}
            />
            {filtered ? (
              <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={clearTextFilter}>
                Clear filters
              </Button>
            ) : null}
            <p role="status" className="ml-auto self-center text-13 text-fg-muted">
              {count === null ? "" : `${count} ${fetched.length === 1 && !filtered ? "service" : "services"}`}
            </p>
          </div>

          <ServicesTable
            services={shown}
            caption={filtered ? "Services matching the filters" : "Services"}
            loading={services.isPending}
            rowActions={(service) => <ServiceRowActions service={service} />}
            empty={
              <EmptyState
                title="No service matches"
                description="Nothing on this machine matches these filters."
                action={
                  <Button icon={<X aria-hidden="true" />} onClick={clearTextFilter}>
                    Clear filters
                  </Button>
                }
                className="border-0 py-8"
              />
            }
          />
          <CommandHint command="wasm service list" label="From a terminal" />
        </div>
      )}

      <CreateServiceDialog open={createOpen} onOpenChange={setCreateOpen} />
    </>
  );
}

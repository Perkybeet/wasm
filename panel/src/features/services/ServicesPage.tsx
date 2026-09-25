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
 * `GET /api/services` only ever answers what the store tracks (see `./data`), so unlike the
 * applications list there is no state or type filter to offer: every row is WASM's.
 */
export function ServicesPage({ search, onSearchChange }: ServicesPageProps) {
  const services = useQuery(servicesQuery());
  const [createOpen, setCreateOpen] = useState(false);

  const all = useMemo(() => services.data?.services ?? [], [services.data]);
  const shown = useMemo(() => filterServices(all, search), [all, search]);

  const set = (patch: SearchPatch, replace = false): void => {
    const next: SearchPatch = { ...search, ...patch };
    const clean: ServicesSearch = {};
    if (next.q) clean.q = next.q;
    onSearchChange(clean, { replace });
  };

  const filtered = isFiltered(search);
  const count = services.data ? (filtered ? `${String(shown.length)} of ${String(all.length)}` : String(all.length)) : null;

  return (
    <>
      <PageHeader
        title="Services"
        description="The systemd units WASM manages on this machine: units created by other packages are not listed here today."
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
      ) : services.data !== undefined && all.length === 0 ? (
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
          <div role="search" aria-label="Filter services" className="flex flex-wrap items-end gap-2">
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
            {filtered ? (
              <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
                Clear filters
              </Button>
            ) : null}
            <p role="status" className="ml-auto self-center text-13 text-fg-muted">
              {count === null ? "" : `${count} ${all.length === 1 && !filtered ? "service" : "services"}`}
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
                  <Button icon={<X aria-hidden="true" />} onClick={() => onSearchChange({})}>
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

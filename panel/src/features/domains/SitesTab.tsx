import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { FileCode, Globe, Lock, MoreHorizontal, Play, Plus, RotateCw, Search, Square, Trash2, X } from "lucide-react";
import { useState } from "react";

import { request } from "../../api/client";
import { sitesQuery } from "../../api/queries/sites";
import type { SiteEntry } from "../../api/queries/sites";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import type { Column } from "../../components/ui/DataTable";
import { DataTable } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { Menu, MenuItem, MenuSeparator } from "../../components/ui/Menu";
import { StatusPill } from "../../components/ui/StatusPill";
import { toast } from "../../components/ui/toast";
import { CreateSiteDialog } from "./CreateSiteDialog";
import { truncatedNames } from "./names";
import { useSiteActions } from "./useSiteActions";

/** Whether a site serves HTTPS, as an attribute of it rather than a state: no colour. */
export function Tls({ secure }: { secure: boolean }) {
  return secure ? (
    <span className="inline-flex items-center gap-1.5 text-fg">
      <Lock aria-hidden="true" className="size-3.5 text-fg-muted" />
      HTTPS
    </span>
  ) : (
    <span className="text-fg-muted">HTTP only</span>
  );
}

export function SiteState({ enabled }: { enabled: boolean }) {
  return <StatusPill state={enabled ? "running" : "stopped"} label={enabled ? "Enabled" : "Disabled"} appearance="inline" size="sm" />;
}

/** The names a site's configuration answers on, mono and joined, truncated past three with a
 * "+N more" tail; the full list is always in the title. */
function ServedNames({ names }: { names: readonly string[] }) {
  if (names.length === 0) return <span className="text-fg-faint">Unknown</span>;
  const { shown, rest } = truncatedNames(names);
  return (
    <span className="flex min-w-0 items-center gap-1.5" title={names.join(", ")}>
      <span translate="no" className="mono truncate text-12 text-fg-muted">
        {shown}
      </span>
      {rest > 0 ? <span className="shrink-0 text-12 text-fg-faint">{`+${String(rest)} more`}</span> : null}
    </span>
  );
}

/**
 * Every web server site on this machine: which server serves it, whether it is enabled and
 * serves HTTPS, and where its file is. A site opens its configuration editor.
 */
export function SitesTab() {
  const navigate = useNavigate();
  const sites = useQuery(sitesQuery());
  const { enable, disable, reload, refresh } = useSiteActions();
  const [filter, setFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<SiteEntry | null>(null);

  const all = sites.data?.sites ?? [];
  const webserver = sites.data?.webserver ?? "nginx";
  const needle = filter.trim().toLowerCase();
  const shown = needle === "" ? all : all.filter((site) => site.name.includes(needle));
  const open = (site: SiteEntry): void => {
    void navigate({ to: "/domains/sites/$site", params: { site: site.name } });
  };

  const columns: Column<SiteEntry>[] = [
    {
      id: "name",
      header: "Site",
      cell: (site) => (
        <span translate="no" className="whitespace-nowrap">
          {site.name}
        </span>
      ),
      sortValue: (site) => site.name,
    },
    { id: "server", header: "Server", mono: true, hideBelow: "sm", cell: (site) => site.webserver, sortValue: (site) => site.webserver },
    { id: "state", header: "State", cell: (site) => <SiteState enabled={site.enabled} />, sortValue: (site) => (site.enabled ? 0 : 1) },
    { id: "tls", header: "Serves", hideBelow: "md", cell: (site) => <Tls secure={site.has_ssl} />, sortValue: (site) => (site.has_ssl ? 0 : 1) },
    // The file each site lives in is on the site's own page: here the width goes to the names.
    { id: "names", header: "Names", hideBelow: "lg", cell: (site) => <ServedNames names={site.server_names} /> },
  ];

  const createButton = (
    <Button variant="primary" icon={<Plus aria-hidden="true" />} onClick={() => setCreating(true)}>
      Create site
    </Button>
  );

  return (
    <div className="flex flex-col gap-4">
      {sites.isError && sites.data === undefined ? (
        <ErrorBlock error={sites.error} title="Could not list the sites" onRetry={() => void sites.refetch()} retrying={sites.isRefetching} />
      ) : sites.data !== undefined && all.length === 0 ? (
        <EmptyState
          level={3}
          icon={<Globe />}
          title="No sites yet"
          description="A site tells the web server how to answer a name. Every app gets one when it is deployed; create one here for a name that is not an app."
          action={createButton}
          command="wasm site create --domain example.com"
          className="py-16"
        />
      ) : (
        <>
          <div role="search" aria-label="Filter sites" className="flex flex-wrap items-center gap-2">
            <Input
              type="search"
              aria-label="Filter sites by name"
              placeholder="Filter by name"
              value={filter}
              onValueChange={(value: string) => setFilter(value)}
              icon={<Search />}
              className="w-full sm:w-64"
              autoComplete="off"
              spellCheck={false}
            />
            {filter !== "" ? (
              <Button variant="ghost" icon={<X aria-hidden="true" />} onClick={() => setFilter("")}>
                Clear
              </Button>
            ) : null}
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <Button icon={<RotateCw aria-hidden="true" />} loading={reload.isPending} onClick={() => reload.mutate()}>
                {`Test and reload ${webserver}`}
              </Button>
              {createButton}
            </div>
          </div>
          <DataTable
            caption={needle === "" ? "Sites" : "Sites matching the filter"}
            columns={columns}
            rows={shown}
            getRowId={(site) => `${site.webserver}:${site.name}`}
            loading={sites.isPending}
            onRowActivate={open}
            empty={<EmptyState title="No site matches" description={`No site's name contains "${filter.trim()}".`} className="border-0 py-8" />}
            rowActions={(site) => (
              <Menu align="end" trigger={<IconButton label={`Actions for ${site.name}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}>
                <MenuItem icon={<FileCode />} onClick={() => open(site)}>
                  Edit configuration
                </MenuItem>
                {site.enabled ? (
                  <MenuItem icon={<Square />} disabled={disable.isPending} onClick={() => disable.mutate(site.name)}>
                    Disable
                  </MenuItem>
                ) : (
                  <MenuItem icon={<Play />} disabled={enable.isPending} onClick={() => enable.mutate(site.name)}>
                    Enable
                  </MenuItem>
                )}
                <MenuSeparator />
                <MenuItem icon={<Trash2 />} destructive onClick={() => setDeleting(site)}>
                  Delete
                </MenuItem>
              </Menu>
            )}
          />
          {/* Drawn with the rows, not before: under a list of unknown length it would only be
              pushed down the page when they arrive. */}
          {sites.isPending ? null : <CommandHint command="wasm site list" label="From a terminal" />}
        </>
      )}

      <CreateSiteDialog
        open={creating}
        onOpenChange={setCreating}
        detected={webserver}
        onCreated={(site, message) => {
          refresh(site);
          if (message.includes("TLS was requested but not enabled")) {
            toast.warning(`Created ${site} without HTTPS`, { detail: message });
          } else {
            toast.success(`Created ${site}`, { description: message });
          }
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(next) => {
          if (!next) setDeleting(null);
        }}
        title={deleting ? `Delete ${deleting.name}` : "Delete site"}
        description="Its nginx and Apache configuration is removed, whichever exists, together with its certificate, and the web server reloads without it. An app behind it keeps running but is no longer reachable by this name."
        confirmText={deleting?.name ?? ""}
        actionLabel="Delete site"
        onConfirm={async () => {
          if (!deleting) return;
          await request("delete", "/api/sites/{domain}", { params: { domain: deleting.name } });
          refresh(deleting.name);
          toast.success(`Deleted ${deleting.name}`);
        }}
      />
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useBlocker, useNavigate } from "@tanstack/react-router";
import { CircleAlert, CornerDownRight, FileX, MoreHorizontal, Play, RotateCw, ShieldCheck, Square, Trash2, Undo2 } from "lucide-react";
import { useId, useRef, useState } from "react";

import { ElevationCancelledError, isApiError, request } from "../../api/client";
import { siteConfigQuery, siteKeys, siteQuery } from "../../api/queries/sites";
import type { SiteConfig } from "../../api/queries/sites";
import { PageHeader } from "../../app/PageHeader";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section, Sections } from "../../components/page/Section";
import { Button, buttonClassName } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { Dialog } from "../../components/ui/Dialog";
import { EmptyState } from "../../components/ui/EmptyState";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem } from "../../components/ui/Menu";
import { Skeleton } from "../../components/ui/Skeleton";
import { toast } from "../../components/ui/toast";
import { ConfigEditor } from "./ConfigEditor";
import type { ConfigEditorHandle } from "./ConfigEditor";
import { SiteState, Tls } from "./SitesTab";
import { configRejection } from "./configErrors";
import type { ConfigRejection } from "./configErrors";
import { useSiteActions } from "./useSiteActions";

const BREADCRUMBS = [{ label: "Domains and certificates", to: "/domains" }] as const;

type Outcome = { kind: "saved"; webserver: string } | { kind: "rejected"; rejection: ConfigRejection } | null;

function Rejected({ rejection, id, onGoToLine }: { rejection: ConfigRejection; id: string; onGoToLine: (line: number) => void }) {
  return (
    <div id={id} role="alert" className="flex min-w-0 flex-col gap-2 rounded-card border border-fail/30 bg-fail-soft/50 p-4">
      <div className="flex items-start gap-2">
        <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-fail" />
        <div className="flex min-w-0 flex-col gap-1">
          <p className="text-13 font-medium text-fg">Nothing was saved: the configuration test failed.</p>
          <p className="text-13 text-pretty text-fg-muted">
            {`${rejection.summary}. The file on disk is unchanged and the web server keeps serving it. Fix the line it names and save again.`}
          </p>
        </div>
      </div>
      {/* Focusable: a long output scrolls, and a keyboard scrolls what has focus. */}
      {/* eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex */}
      <pre tabIndex={0} className="max-h-56 overflow-auto rounded-control border border-border bg-surface px-3 py-2 text-12 whitespace-pre-wrap break-words text-fg scroll-thin">
        {rejection.output.trim() === "" ? rejection.summary : rejection.output.trimEnd()}
      </pre>
      {rejection.line !== null ? (
        <div>
          <Button size="sm" icon={<CornerDownRight aria-hidden="true" />} onClick={() => onGoToLine(rejection.line ?? 1)}>
            {`Go to line ${String(rejection.line)}`}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function Saved({ webserver, id, onReload, reloading }: { webserver: string; id: string; onReload: () => void; reloading: boolean }) {
  return (
    <div id={id} role="status" className="flex flex-col gap-3 rounded-card border border-ok/30 bg-ok-soft/40 p-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-2">
        <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-ok" />
        <p className="text-13 text-pretty text-fg">
          {`Saved. It passed ${webserver}'s configuration test; ${webserver} serves the previous version until it reloads.`}
        </p>
      </div>
      <Button icon={<RotateCw aria-hidden="true" />} loading={reloading} onClick={onReload} className="self-start sm:self-auto">
        {`Reload ${webserver}`}
      </Button>
    </div>
  );
}

function EditorSkeleton() {
  return (
    <div aria-busy="true" className="flex flex-col gap-2 rounded-control border border-border p-3">
      <span className="sr-only">Loading the configuration</span>
      {["w-2/5", "w-2/3", "w-1/2", "w-3/4", "w-1/3", "w-3/5", "w-1/2", "w-2/3"].map((width, index) => (
        <Skeleton key={index} className={`h-3 ${width}`} />
      ))}
    </div>
  );
}

/**
 * One web server site: its state, and its configuration file in an editor. Saving always tests
 * the text with the web server first; a refusal is shown in the server's own words, the line it
 * names is marked, and the file on disk is left as it was.
 */
export function SiteConfigPage({ site }: { site: string }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const info = useQuery(siteQuery(site));
  const config = useQuery(siteConfigQuery(site));
  const { enable, disable, reload, refresh } = useSiteActions();
  const editor = useRef<ConfigEditorHandle>(null);
  const outcomeId = useId();
  const pathId = useId();

  const [draft, setDraft] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [deleting, setDeleting] = useState(false);

  const saved = config.data?.config ?? "";
  const text = draft ?? saved;
  const dirty = draft !== null && draft !== saved;
  const webserver = config.data?.webserver ?? info.data?.webserver ?? "nginx";

  // Set once the site is deleted: there is nothing left to lose by leaving.
  const gone = useRef(false);
  const blocker = useBlocker({
    // Signing in again after the session expired is not leaving: the draft could not be saved.
    shouldBlockFn: ({ next }) => dirty && !gone.current && next.pathname !== "/login",
    enableBeforeUnload: () => dirty && !gone.current,
    withResolver: true,
  });

  const save = useMutation({
    mutationFn: (next: string) => request("put", "/api/sites/{domain}/config", { params: { domain: site }, body: { config: next } }),
    onMutate: () => {
      setOutcome(null);
    },
    onSuccess: (_result, next) => {
      queryClient.setQueryData<SiteConfig>(siteKeys.config(site), (current) => (current ? { ...current, config: next } : current));
      setDraft(null);
      setOutcome({ kind: "saved", webserver });
      refresh(site);
    },
    onError: (error) => {
      const rejection = configRejection(error);
      if (rejection !== null) setOutcome({ kind: "rejected", rejection });
    },
  });

  const onChange = (value: string): void => {
    setDraft(value);
    if (outcome?.kind === "saved") setOutcome(null);
  };

  if ((info.isError && isApiError(info.error) && info.error.status === 404) || (config.isError && isApiError(config.error) && config.error.status === 404)) {
    return (
      <>
        <PageHeader title={site} breadcrumbs={BREADCRUMBS} />
        <EmptyState
          level={2}
          icon={<FileX />}
          title="No site with this name"
          description="It may have been deleted, or the address has a typo. Every site on this machine is in the Sites tab."
          action={
            <Link to="/domains" search={{ tab: "sites" }} className={buttonClassName("secondary")}>
              All sites
            </Link>
          }
          command="wasm site list"
          className="py-16"
        />
      </>
    );
  }

  const facts = info.data ? (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <SiteState enabled={info.data.enabled} />
      <span translate="no" className="mono text-12 text-fg-muted">
        {info.data.webserver}
      </span>
      <span className="text-13">
        <Tls secure={info.data.has_ssl} />
      </span>
    </span>
  ) : (
    <span aria-hidden="true" className="flex items-center gap-3">
      <Skeleton className="h-4 w-20" />
      <Skeleton className="h-3 w-12" />
    </span>
  );

  const actions = info.data ? (
    <>
      {info.data.enabled ? (
        <Button icon={<Square aria-hidden="true" />} loading={disable.isPending} onClick={() => disable.mutate(site)}>
          Disable
        </Button>
      ) : (
        <Button icon={<Play aria-hidden="true" />} loading={enable.isPending} onClick={() => enable.mutate(site)}>
          Enable
        </Button>
      )}
      <Menu align="end" trigger={<IconButton variant="secondary" label="More actions" icon={<MoreHorizontal />} tooltip={false} />}>
        <MenuItem icon={<Trash2 />} destructive onClick={() => setDeleting(true)}>
          Delete site
        </MenuItem>
      </Menu>
    </>
  ) : undefined;

  const describedBy = [pathId, outcome ? outcomeId : null].filter(Boolean).join(" ");

  return (
    <>
      <PageHeader title={site} breadcrumbs={BREADCRUMBS} description={facts} actions={actions} />
      <Sections>
        <Section
          title="Configuration"
          description={
            <>
              {"Saving tests it with "}
              <code className="text-12">{webserver === "apache" ? "apache2ctl -t" : "nginx -t"}</code>
              {" first. A configuration the web server rejects is not saved, and the live one keeps serving."}
            </>
          }
        >
          {config.isError && config.data === undefined ? (
            <ErrorBlock error={config.error} title={`Could not read the configuration of ${site}`} onRetry={() => void config.refetch()} retrying={config.isRefetching} />
          ) : config.data === undefined ? (
            <EditorSkeleton />
          ) : (
            <div className="flex flex-col gap-3">
              <p id={pathId} className="flex min-w-0 items-center gap-2 text-12 text-fg-muted">
                <span className="shrink-0">File</span>
                <code translate="no" className="truncate text-fg" title={config.data.path}>
                  {config.data.path}
                </code>
              </p>
              <ConfigEditor
                ref={editor}
                value={text}
                onChange={onChange}
                label={`Configuration of ${site}`}
                describedBy={describedBy}
                errorLine={outcome?.kind === "rejected" ? outcome.rejection.line : null}
                disabled={save.isPending}
              />
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p role="status" className="text-13 text-fg-muted">
                  {dirty ? "Unsaved changes" : outcome?.kind === "saved" ? "" : "No changes"}
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="ghost" icon={<Undo2 aria-hidden="true" />} disabled={!dirty || save.isPending} onClick={() => { setDraft(null); setOutcome(null); }}>
                    Discard changes
                  </Button>
                  <Button variant="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate(text)}>
                    Test and save
                  </Button>
                </div>
              </div>
              {outcome?.kind === "rejected" ? (
                <Rejected rejection={outcome.rejection} id={outcomeId} onGoToLine={(line) => editor.current?.goToLine(line)} />
              ) : outcome?.kind === "saved" ? (
                <Saved webserver={outcome.webserver} id={outcomeId} onReload={() => reload.mutate()} reloading={reload.isPending} />
              ) : save.isError && !(save.error instanceof ElevationCancelledError) ? (
                <ErrorBlock live error={save.error} title="The configuration was not saved" />
              ) : save.error instanceof ElevationCancelledError ? (
                <p role="status" className="text-13 text-fg-muted">{save.error.detail}</p>
              ) : null}
            </div>
          )}
        </Section>
        <CommandHint command={`wasm site show ${site}`} label="From a terminal" />
      </Sections>

      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete ${site}`}
        description="Its nginx and Apache configuration is removed, whichever exists, together with its certificate, and the web server reloads without it. An app behind it keeps running but is no longer reachable by this name."
        confirmText={site}
        actionLabel="Delete site"
        onConfirm={async () => {
          await request("delete", "/api/sites/{domain}", { params: { domain: site } });
          gone.current = true;
          refresh(site);
          toast.success(`Deleted ${site}`);
          void navigate({ to: "/domains", search: { tab: "sites" } });
        }}
      />
      <Dialog
        open={blocker.status === "blocked"}
        onOpenChange={(open) => {
          if (!open) blocker.reset?.();
        }}
        size="sm"
        title="Leave without saving?"
        description={`Your changes to the configuration of ${site} have not been saved or tested. Leaving discards them.`}
        footer={
          <>
            <Button onClick={() => blocker.reset?.()}>Keep editing</Button>
            <Button variant="danger" onClick={() => blocker.proceed?.()}>
              Discard changes
            </Button>
          </>
        }
      />
    </>
  );
}

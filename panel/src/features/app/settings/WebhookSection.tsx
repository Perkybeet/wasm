import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { KeyRound, Webhook } from "lucide-react";
import { useId, useState } from "react";

import { request } from "../../../api/client";
import type { ResponseOf } from "../../../api/client";
import { appKeys, webhookDeliveriesQuery } from "../../../api/queries/apps";
import type { App, WebhookDelivery } from "../../../api/queries/apps";
import { announce } from "../../../app/Announcer";
import { DeployStatePill } from "../../../components/page/AppStatePill";
import { ErrorBlock } from "../../../components/page/QueryState";
import { RelativeTime } from "../../../components/page/RelativeTime";
import { Section } from "../../../components/page/Section";
import { Button } from "../../../components/ui/Button";
import { CopyButton } from "../../../components/ui/CopyButton";
import { DataTable } from "../../../components/ui/DataTable";
import type { Column } from "../../../components/ui/DataTable";
import { Dialog } from "../../../components/ui/Dialog";
import { StatusPill } from "../../../components/ui/StatusPill";
import { toast } from "../../../components/ui/toast";
import { formatCount } from "../../../lib/format";
import { LINK, PANEL } from "./panel";

type WebhookSecret = ResponseOf<"/api/apps/{domain}/webhook-secret", "post">;

/** How many deliveries the tab lists; the rest are on the Deployments tab. */
export const DELIVERIES_SHOWN = 10;

/** The first line of a failure, which is what fits in a row; the deployment has the rest. */
export function firstLine(text: string | null | undefined): string | null {
  const line = text
    ?.split("\n")
    .map((part) => part.trim())
    .find((part) => part !== "");
  return line ?? null;
}

/** A value the operator copies into the forge: in full, never truncated, with its copy button. */
function CopyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid min-w-0 gap-1 sm:grid-cols-[7rem_minmax(0,1fr)] sm:items-center sm:gap-4">
      <span className="text-13 text-fg-muted">{label}</span>
      <div className="flex min-w-0 items-center gap-1 rounded-control border border-border bg-bg-sunken py-1 pr-1 pl-3">
        <code translate="no" className="min-w-0 flex-1 text-12 break-all text-fg">
          {value}
        </code>
        <CopyButton value={value} label={`Copy ${label.charAt(0).toLowerCase()}${label.slice(1)}`} />
      </div>
    </div>
  );
}

/** The secret just created, shown this once, with what to paste where in each forge. */
function NewSecret({ secret, onDone }: { secret: WebhookSecret; onDone: () => void }) {
  const titleId = useId();
  return (
    <div role="region" aria-labelledby={titleId} className="flex flex-col gap-4 rounded-control border border-border-strong bg-surface-raised p-4">
      <div className="flex items-start gap-2.5">
        <KeyRound aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-fg-muted" />
        <div className="flex flex-col gap-0.5">
          <h3 id={titleId} className="text-14 font-medium text-fg">
            Copy the secret now
          </h3>
          <p className="text-13 text-pretty text-fg-muted">
            It is shown this once. WASM keeps it only to check the signature of each delivery.
          </p>
        </div>
      </div>
      <div className="flex flex-col gap-2.5">
        <CopyRow label="Payload URL" value={secret.hook_url} />
        <CopyRow label="Secret" value={secret.secret} />
      </div>
      <div className="grid gap-4 border-t border-border pt-4 text-13 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <h4 className="font-medium text-fg">GitHub or Gitea</h4>
          <p className="text-pretty text-fg-muted">
            In the repository settings, add a webhook: the payload URL and the secret above, content type{" "}
            <code className="text-12 text-fg">application/json</code>, only the push event.
          </p>
        </div>
        <div className="flex flex-col gap-1">
          <h4 className="font-medium text-fg">GitLab</h4>
          <p className="text-pretty text-fg-muted">
            In the project settings, add a webhook: the URL above, the secret as its secret token, push events.
          </p>
        </div>
      </div>
      <div>
        <Button onClick={onDone}>Hide the secret</Button>
      </div>
    </div>
  );
}

/** What a delivery without an error came to, by its deployment's status. */
const OUTCOME: Readonly<Record<string, string>> = {
  success: "Deployed",
  rolled_back: "Deployed, later rolled back",
  running: "Deploying now",
  queued: "Waiting to deploy",
};

function deliveryColumns(domain: string): Column<WebhookDelivery>[] {
  return [
    {
      id: "status",
      header: "Status",
      cell: (row) => <DeployStatePill status={row.status} appearance="inline" size="sm" />,
      width: "w-32",
    },
    {
      id: "commit",
      header: "Commit",
      mono: true,
      cell: (row) => (row.git_commit ? <span translate="no">{row.git_commit.slice(0, 7)}</span> : <span className="text-fg-faint">-</span>),
      width: "w-24",
    },
    {
      id: "started",
      header: "Started",
      cell: (row) => <RelativeTime value={row.started_at} className="text-fg-muted" />,
      width: "w-28",
      hideBelow: "sm",
    },
    {
      id: "outcome",
      header: "Outcome",
      cell: (row) => {
        const line = firstLine(row.error);
        return line !== null ? (
          <code translate="no" title={line} className="block max-w-[32ch] truncate text-12 text-fg-muted lg:max-w-[52ch]">
            {line}
          </code>
        ) : (
          <span className="text-fg-muted">{OUTCOME[row.status] ?? "No error recorded"}</span>
        );
      },
      hideBelow: "md",
    },
    {
      id: "deployment",
      header: "Deployment",
      align: "end",
      cell: (row) => (
        <Link to="/apps/$domain/deployments/$id" params={{ domain, id: String(row.deployment_id) }} className={LINK}>
          {`Deploy ${String(row.deployment_id)}`}
        </Link>
      ),
      width: "w-32",
    },
  ];
}

/**
 * Deploys started by a push. Whether a secret is set is known; the secret itself is shown once,
 * when it is created, and never again. Creating another replaces it at once, so that is asked
 * first.
 */
export function WebhookSection({ app }: { app: App }) {
  const domain = app.domain;
  const queryClient = useQueryClient();
  const deliveries = useQuery(webhookDeliveriesQuery(domain));
  const [secret, setSecret] = useState<WebhookSecret | null>(null);
  const [confirm, setConfirm] = useState<"regenerate" | "disable" | null>(null);
  const enabled = app.webhook_enabled;

  const settle = (next: boolean): void => {
    queryClient.setQueryData<App>(appKeys.detail(domain), (known) => (known ? { ...known, webhook_enabled: next } : known));
    void queryClient.invalidateQueries({ queryKey: appKeys.detail(domain), exact: true });
  };

  const create = useMutation({
    mutationFn: () => request("post", "/api/apps/{domain}/webhook-secret", { params: { domain } }),
    onSuccess: (result) => {
      setSecret(result);
      setConfirm(null);
      settle(true);
      announce("Webhook secret created. Copy it now: it is shown this once.");
    },
  });
  const disable = useMutation({
    mutationFn: () => request("delete", "/api/apps/{domain}/webhook-secret", { params: { domain } }),
    onSuccess: () => {
      setSecret(null);
      setConfirm(null);
      settle(false);
      toast.success(`Webhook of ${domain} disabled`);
    },
  });

  const items = deliveries.data?.items ?? [];
  const total = deliveries.data?.total ?? 0;
  const latest = items[0];

  const status = enabled ? <StatusPill state="running" label="Enabled" size="sm" /> : <StatusPill state="stopped" label="Disabled" size="sm" />;

  const summary = !enabled
    ? "Deliveries are refused until a secret is created."
    : latest
      ? null
      : deliveries.isPending
        ? "Reading the deliveries…"
        : "No push has deployed this app yet.";

  const closeConfirm = (next: boolean): void => {
    if (!next && (create.isPending || disable.isPending)) return;
    if (!next) {
      setConfirm(null);
      create.reset();
      disable.reset();
    }
  };

  return (
    <Section title="Deploy webhook" description="A push to the repository deploys the app. The forge signs each delivery with a secret WASM checks.">
      <div className={`${PANEL} flex flex-col gap-4 px-4 py-4`}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex min-w-0 items-center gap-3">
            <Webhook aria-hidden="true" className="size-4 shrink-0 text-fg-faint" />
            {status}
            {summary !== null ? (
              <p className="text-13 text-fg-muted">{summary}</p>
            ) : latest ? (
              <p className="text-13 text-fg-muted">
                {"Last delivery "}
                <RelativeTime value={latest.started_at} className="text-fg" />
                {`. ${formatCount(total)} ${total === 1 ? "delivery" : "deliveries"} in total.`}
              </p>
            ) : null}
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            {enabled ? (
              <>
                <Button variant="ghost" onClick={() => setConfirm("disable")}>
                  Disable webhook
                </Button>
                <Button variant={secret ? "secondary" : "primary"} onClick={() => setConfirm("regenerate")}>
                  Regenerate secret
                </Button>
              </>
            ) : (
              <Button variant="primary" loading={create.isPending} onClick={() => create.mutate()}>
                Enable webhook
              </Button>
            )}
          </div>
        </div>
        {create.isError && confirm === null ? <ErrorBlock live compact error={create.error} title="The secret was not created" /> : null}
        {secret !== null ? <NewSecret secret={secret} onDone={() => setSecret(null)} /> : null}
      </div>

      <div className="flex min-w-0 flex-col gap-3">
        <h3 className="text-14 font-medium text-fg">Recent deliveries</h3>
        {deliveries.isError && deliveries.data === undefined ? (
          <ErrorBlock compact error={deliveries.error} title="Could not load the deliveries" onRetry={() => void deliveries.refetch()} />
        ) : (
          <DataTable
            caption={`Deploys started by a push to ${domain}, newest first`}
            columns={deliveryColumns(domain)}
            rows={items.slice(0, DELIVERIES_SHOWN)}
            getRowId={(row) => String(row.deployment_id)}
            loading={deliveries.isPending}
            density="compact"
            empty={<p className="text-13 text-fg-muted">No push has deployed this app yet. Deliveries appear here once the forge sends one.</p>}
          />
        )}
        {total > DELIVERIES_SHOWN ? (
          <p className="text-12 text-fg-muted">
            {`The newest ${String(DELIVERIES_SHOWN)} of ${formatCount(total)}. `}
            <Link to="/apps/$domain/deployments" params={{ domain }} className={LINK}>
              Every deploy is on the Deployments tab
            </Link>
          </p>
        ) : null}
      </div>

      <Dialog
        open={confirm !== null}
        onOpenChange={closeConfirm}
        size="sm"
        title={confirm === "disable" ? `Disable the webhook of ${domain}?` : "Regenerate the secret?"}
        description={
          confirm === "disable"
            ? "The secret is discarded and every delivery is refused until a new one is created. Nothing else changes."
            : "The new secret replaces the current one at once: deliveries signed with the old secret are refused until the forge has the new one."
        }
        footer={
          <>
            <Button disabled={create.isPending || disable.isPending} onClick={() => closeConfirm(false)}>
              Cancel
            </Button>
            {confirm === "disable" ? (
              <Button variant="danger" loading={disable.isPending} onClick={() => disable.mutate()}>
                Disable webhook
              </Button>
            ) : (
              <Button variant="primary" loading={create.isPending} onClick={() => create.mutate()}>
                Regenerate secret
              </Button>
            )}
          </>
        }
      >
        {confirm === "disable" && disable.isError ? (
          <ErrorBlock live compact error={disable.error} title="The webhook was not disabled" />
        ) : confirm === "regenerate" && create.isError ? (
          <ErrorBlock live compact error={create.error} title="The secret was not created" />
        ) : undefined}
      </Dialog>
    </Section>
  );
}

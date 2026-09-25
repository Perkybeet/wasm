import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { apiTokensQuery, authKeys, revokeApiToken } from "../../api/queries/auth";
import type { ApiToken } from "../../api/queries/auth";
import { useDocumentTitle } from "../../app/documentTitle";
import { CommandHint } from "../../components/page/CommandHint";
import { QueryState } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Section, Sections } from "../../components/page/Section";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { StatusGlyph } from "../../components/ui/StatusPill";
import { toast } from "../../components/ui/toast";
import { CreateTokenDialog } from "./CreateTokenDialog";
import { RowAction } from "./RowAction";
import { sortTokens, tokenState } from "./tokens";
import type { TokenState } from "./tokens";

const STATE_LABEL: Record<TokenState, string> = { active: "Active", expired: "Expired", revoked: "Revoked" };

/** A token's state told three ways: colour, shape and word. */
function TokenStateLabel({ state }: { state: TokenState }) {
  return (
    <span className="flex items-center gap-1.5">
      {state === "active" ? (
        <StatusGlyph state="running" className="text-ok" />
      ) : state === "expired" ? (
        <StatusGlyph state="stopped" className="text-idle" />
      ) : (
        <Ban aria-hidden="true" className="size-3 text-idle" />
      )}
      <span className={state === "active" ? "text-fg" : "text-fg-muted"}>{STATE_LABEL[state]}</span>
    </span>
  );
}

const COLUMNS: readonly Column<ApiToken>[] = [
  {
    id: "name",
    header: "Name",
    cell: (token) => (
      <span className="flex flex-col items-start gap-1">
        <span translate="no" className="mono text-12">
          {token.name}
        </span>
        {/* On a phone the scope column is hidden; the scope rides under the name. */}
        <Badge mono className="sm:hidden">
          {token.scope}
        </Badge>
      </span>
    ),
  },
  {
    id: "scope",
    header: "Scope",
    hideBelow: "sm",
    cell: (token) => <Badge mono>{token.scope}</Badge>,
  },
  { id: "state", header: "State", cell: (token) => <TokenStateLabel state={tokenState(token)} /> },
  {
    id: "created",
    header: "Created",
    hideBelow: "md",
    cell: (token) => <RelativeTime value={token.created_at} />,
  },
  {
    id: "expires",
    header: "Expires",
    hideBelow: "sm",
    cell: (token) =>
      token.revoked_at !== null && token.revoked_at !== undefined ? (
        <span className="sr-only">Does not apply to a revoked token</span>
      ) : (
        <RelativeTime value={token.expires_at} fallback="Never" />
      ),
  },
  {
    id: "last-used",
    header: "Last used",
    hideBelow: "lg",
    cell: (token) => <RelativeTime value={token.last_used_at} fallback="Never" />,
  },
];

/** Settings > API tokens: named, scoped credentials for CI and scripts. */
export function TokensSettings() {
  useDocumentTitle("API tokens", 1);
  const queryClient = useQueryClient();
  const query = useQuery(apiTokensQuery());
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<ApiToken | null>(null);
  const [confirming, setConfirming] = useState(false);

  const create = (
    <Button
      variant="primary"
      icon={<Plus aria-hidden="true" />}
      onClick={() => {
        setCreating(true);
      }}
    >
      Create token
    </Button>
  );

  return (
    <Sections>
      <Section
        title="Tokens for automation"
        description="Named tokens for CI and scripts, each with a scope and an optional expiry. A token is shown once, when it is created; after that only its name and scope remain."
        actions={query.data !== undefined && query.data.tokens.length > 0 ? create : undefined}
      >
        <QueryState
          query={query}
          label="API tokens"
          skeleton={<DataTable caption="API tokens" columns={COLUMNS} rows={[]} getRowId={(token) => String(token.id)} loading />}
          isEmpty={(data) => data.tokens.length === 0}
          empty={
            <EmptyState
              icon={<KeyRound />}
              title="No API tokens yet"
              description="Create one for a CI pipeline or a script. It acts with the scope you choose, and you can revoke it at any time."
              action={create}
            />
          }
        >
          {(data) => (
            <DataTable
              caption="API tokens"
              columns={COLUMNS}
              rows={sortTokens(data.tokens)}
              getRowId={(token) => String(token.id)}
              rowActions={(token) =>
                tokenState(token) === "active" ? (
                  <RowAction
                    label={`Revoke ${token.name}`}
                    text="Revoke"
                    icon={<Trash2 />}
                    onClick={() => {
                      setRevoking(token);
                      setConfirming(true);
                    }}
                  />
                ) : null
              }
            />
          )}
        </QueryState>
      </Section>
      <Section
        title="Using a token"
        description="Send it in the Authorization header. A token cannot open the console in a browser; it is for the API only."
      >
        <CommandHint command={`curl -H "Authorization: Bearer wasm_tok_..." ${window.location.origin}/api/apps`} />
      </Section>

      <CreateTokenDialog
        open={creating}
        onClose={() => {
          setCreating(false);
        }}
      />
      {revoking !== null ? (
        <ConfirmDialog
          open={confirming}
          onOpenChange={setConfirming}
          title={`Revoke ${revoking.name}`}
          description="Requests that present this token stop working at once. It cannot be turned back on, and its name stays taken so the audit log always names one token."
          confirmText={revoking.name}
          actionLabel="Revoke token"
          onConfirm={async () => {
            const result = await revokeApiToken(revoking.id);
            toast.success(`Revoked token ${result.revoked}`);
            void queryClient.invalidateQueries({ queryKey: authKeys.tokens });
          }}
        />
      ) : null}
    </Sections>
  );
}

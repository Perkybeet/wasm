import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { useState } from "react";

import { isApiError } from "../../api/client";
import { authKeys, revokeOtherSessions, revokeSession, sessionsQuery } from "../../api/queries/auth";
import type { ActiveSession } from "../../api/queries/auth";
import { QueryState } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { Dialog } from "../../components/ui/Dialog";
import { SystemOutput } from "../../components/ui/SystemOutput";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";
import { RowAction } from "./RowAction";
import { SettingsSection } from "./SettingsForm";

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
}

/** How many sessions the server reports revoked, from `POST /api/auth/sessions/revoke-others`'s own words. */
function countRevoked(message: string): number | null {
  const match = /(\d+)/.exec(message);
  return match ? Number(match[1]) : null;
}

/** Everyone signed in to this console right now, and signing them out. */
export function SessionsSection() {
  const queryClient = useQueryClient();
  const query = useQuery(sessionsQuery());
  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: authKeys.sessions });
  };

  const revokeOne = useMutation({
    mutationFn: (session: ActiveSession) => revokeSession(session.sid_prefix),
    onSuccess: (_, session) => {
      toast.success(`Signed out session ${session.sid_prefix}`);
      refresh();
    },
    onError: (error, session) => {
      reportActionError(`Could not sign out session ${session.sid_prefix}`, error);
      refresh();
    },
  });

  const others = (query.data?.sessions ?? []).filter((session) => !session.is_current);
  const [confirming, setConfirming] = useState(false);
  const [failure, setFailure] = useState<{ hint: string; detail: string } | null>(null);

  const revokeOthers = useMutation({
    mutationFn: revokeOtherSessions,
    onSuccess: (result) => {
      refresh();
      setConfirming(false);
      const count = countRevoked(result.message);
      toast.success(count === null ? result.message : `Signed out ${plural(count, "other session", "other sessions")}`);
    },
    onError: (error: unknown) => {
      // The one credential this cannot apply to: a Bearer or the master token, which never
      // had a browser tab of its own. The backend answers a bare 400 with no hint of its own.
      if (isApiError(error) && error.status === 400) {
        setFailure({
          hint: "This console is signed in with an API token, not a browser session. Sign in through the browser to use this.",
          detail: error.detail,
        });
        return;
      }
      setConfirming(false);
      reportActionError("Could not sign out other sessions", error);
    },
  });

  const closeConfirm = (next: boolean): void => {
    if (!next && revokeOthers.isPending) return;
    setConfirming(next);
    if (!next) setFailure(null);
  };

  const columns: Column<ActiveSession>[] = [
    {
      id: "session",
      header: "Session",
      cell: (session) => (
        <span className="flex items-center gap-2">
          <span translate="no" className="mono text-12">
            {session.sid_prefix}
          </span>
          {session.is_current ? <Badge>This browser</Badge> : null}
        </span>
      ),
    },
    { id: "address", header: "Address", mono: true, hideBelow: "sm", cell: (session) => session.client_ip },
    {
      id: "signed-in",
      header: "Signed in",
      hideBelow: "md",
      sortValue: (session) => session.created_at,
      cell: (session) => <RelativeTime value={session.created_at} />,
    },
    {
      id: "last-seen",
      header: "Last active",
      sortValue: (session) => session.last_seen,
      cell: (session) => <RelativeTime value={session.last_seen} />,
    },
    {
      id: "expires",
      header: "Expires",
      hideBelow: "sm",
      sortValue: (session) => session.expires_at,
      cell: (session) => <RelativeTime value={session.expires_at} />,
    },
  ];

  return (
    <SettingsSection
      title="Signed-in sessions"
      description="Every browser and client signed in to this console. Signing one out ends it at its next request; your own ends with Sign out, in the menu at the top right."
    >
      <div className="flex min-w-0 flex-col gap-3">
        <QueryState
          query={query}
          label="sessions"
          // One row, the least there can be (this one), compact like the loaded table; the line
          // under it has its place held too.
          skeleton={
            <div className="flex min-w-0 flex-col gap-3">
              <DataTable
                caption="Active sessions"
                columns={columns}
                rows={[]}
                getRowId={(s) => s.sid_prefix}
                density="compact"
                rowActions={() => null}
                loading
                skeletonRows={1}
              />
              <div aria-hidden="true" className="h-8" />
            </div>
          }
        >
          {(data) => (
            <DataTable
              caption="Active sessions"
              columns={columns}
              rows={data.sessions}
              getRowId={(session) => session.sid_prefix}
              density="compact"
              rowActions={(session) =>
                session.is_current ? null : (
                  <RowAction
                    label={`Sign out session ${session.sid_prefix}`}
                    text="Sign out"
                    icon={<LogOut />}
                    loading={revokeOne.isPending && revokeOne.variables.sid_prefix === session.sid_prefix}
                    onClick={() => {
                      revokeOne.mutate(session);
                    }}
                  />
                )
              }
            />
          )}
        </QueryState>
        {query.data !== undefined ? (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-13 text-fg-muted tabular-nums">
              {plural(query.data.active_sessions, "active session", "active sessions")}
            </p>
            <Button
              icon={<LogOut aria-hidden="true" />}
              disabled={others.length === 0}
              onClick={() => {
                setFailure(null);
                setConfirming(true);
              }}
            >
              Sign out other sessions
            </Button>
          </div>
        ) : null}
        <Dialog
          open={confirming}
          onOpenChange={closeConfirm}
          size="sm"
          title="Sign out other sessions?"
          description={`Ends every session but this one${others.length > 0 ? ` — ${plural(others.length, "session", "sessions")}` : ""}. Anyone using them will need to sign in again.`}
          footer={
            <>
              <Button disabled={revokeOthers.isPending} onClick={() => closeConfirm(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={revokeOthers.isPending}
                onClick={() => {
                  revokeOthers.mutate();
                }}
              >
                Sign out other sessions
              </Button>
            </>
          }
        >
          {failure !== null ? (
            <div role="alert" className="flex flex-col gap-2 rounded-control border border-fail/30 bg-fail-soft p-3">
              <p className="text-13 font-medium text-fail">{failure.hint}</p>
              <SystemOutput label="What the server said" maxHeight="max-h-40">
                {failure.detail}
              </SystemOutput>
            </div>
          ) : undefined}
        </Dialog>
      </div>
    </SettingsSection>
  );
}

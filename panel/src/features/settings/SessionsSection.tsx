import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut } from "lucide-react";

import { authKeys, revokeSession, sessionsQuery } from "../../api/queries/auth";
import type { ActiveSession } from "../../api/queries/auth";
import { QueryState } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";
import { RowAction } from "./RowAction";
import { SettingsSection } from "./SettingsForm";

/** Signs out the given sessions one by one: the per-session endpoint is the one chokepoint. */
async function revokeEach(prefixes: readonly string[]): Promise<{ revoked: number; failed: unknown }> {
  let revoked = 0;
  for (const prefix of prefixes) {
    try {
      await revokeSession(prefix);
      revoked += 1;
    } catch (error: unknown) {
      return { revoked, failed: error };
    }
  }
  return { revoked, failed: null };
}

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`;
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
  const revokeOthers = useMutation({
    mutationFn: () => revokeEach(others.map((session) => session.sid_prefix)),
    onSuccess: ({ revoked, failed }) => {
      refresh();
      if (failed !== null) {
        reportActionError(`Signed out ${plural(revoked, "session", "sessions")}, then stopped`, failed);
        return;
      }
      toast.success(`Signed out ${plural(revoked, "other session", "other sessions")}`);
    },
  });

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
          skeleton={<DataTable caption="Active sessions" columns={columns} rows={[]} getRowId={(s) => s.sid_prefix} loading />}
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
              loading={revokeOthers.isPending}
              onClick={() => {
                revokeOthers.mutate();
              }}
            >
              Sign out other sessions
            </Button>
          </div>
        ) : null}
      </div>
    </SettingsSection>
  );
}

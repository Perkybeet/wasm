import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import { appKeys } from "../../api/queries/apps";
import { authKeys } from "../../api/queries/auth";
import type { SessionInfo } from "../../api/queries/auth";
import { jobKeys } from "../../api/queries/jobs";
import { announce } from "../../app/Announcer";
import { elevate } from "../auth/elevation";

export interface DeleteAppOptions {
  /** Also delete the application's directory. */
  removeFiles: boolean;
  /** Also delete its certificate. */
  removeSsl: boolean;
}

/**
 * Whether the session can run a sudo-mode action without asking. Only cookie sessions are ever
 * asked (API tokens with admin scope are exempt server-side), and a console is always one.
 */
export function isElevated(session: SessionInfo | undefined, now: number = Date.now()): boolean {
  if (session?.elevated_until === null || session?.elevated_until === undefined) return false;
  const until = Date.parse(session.elevated_until);
  return Number.isFinite(until) && until > now;
}

/**
 * Asks "Confirm it's you" up front, before a dialog that runs a sudo-mode action opens. The
 * client would ask anyway when the action answers elevation_required, but that second dialog
 * would then open on top of the first; asking first keeps one dialog on screen at a time.
 * Resolves at once when the session is already elevated; rejects with
 * ElevationCancelledError when the operator declines.
 */
export function useConfirmItsYou(): () => Promise<void> {
  const queryClient = useQueryClient();
  return async () => {
    if (isElevated(queryClient.getQueryData<SessionInfo>(authKeys.session))) return;
    await elevate();
  };
}

/**
 * Deletes an application: `DELETE /api/apps/{domain}`, the endpoint behind sudo mode, which
 * queues the deletion as a job. The service, the site and, when asked, the files and the
 * certificate go; backups stay.
 */
export function useDeleteApp(domain: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ removeFiles, removeSsl }: DeleteAppOptions) =>
      request("delete", "/api/apps/{domain}", {
        params: { domain },
        query: { remove_files: removeFiles, remove_ssl: removeSsl },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: jobKeys.active });
      void queryClient.invalidateQueries({ queryKey: appKeys.list, exact: true });
      announce(`Deletion of ${domain} queued`);
    },
  });
}

import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { isApiError } from "../../api/client";
import { logout } from "../../api/queries/auth";
import { announce } from "../../app/Announcer";
import { toast } from "../../components/ui/toast";
import { describeError } from "../../lib/errors";

/** Ends the session, forgets everything it loaded, and shows the sign-in page. */
export function useSignOut(): { signOut: () => Promise<void>; pending: boolean } {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [pending, setPending] = useState(false);

  const signOut = async (): Promise<void> => {
    setPending(true);
    try {
      await logout();
    } catch (error: unknown) {
      setPending(false);
      // A session that already ended is handled by the client: it is on its way to sign-in.
      if (isApiError(error) && error.sessionExpired) return;
      const { hint, detail } = describeError(error);
      toast.error("Sign out failed", { ...(hint !== null ? { description: hint } : {}), detail });
      return;
    }
    queryClient.clear();
    await navigate({ to: "/login", replace: true });
    setPending(false);
    announce("Signed out");
  };

  return { signOut, pending };
}

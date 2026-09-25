import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import { siteKeys } from "../../api/queries/sites";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";

/**
 * What can be done to a site from anywhere it is shown: enable, disable, and test and reload
 * the web server. Deleting goes through a confirmation and is called directly there.
 */
export function useSiteActions() {
  const queryClient = useQueryClient();
  const refresh = (site?: string): void => {
    void queryClient.invalidateQueries({ queryKey: siteKeys.all });
    if (site !== undefined) void queryClient.invalidateQueries({ queryKey: siteKeys.detail(site) });
  };

  const enable = useMutation({
    mutationFn: (site: string) => request("post", "/api/sites/{domain}/enable", { params: { domain: site } }),
    onSuccess: (result, site) => {
      toast.success(`Enabled ${site}`, { description: "The web server reloaded with it." });
      refresh(result.site);
    },
    onError: (error, site) => {
      reportActionError(`Could not enable ${site}`, error);
      refresh(site);
    },
  });

  const disable = useMutation({
    mutationFn: (site: string) => request("post", "/api/sites/{domain}/disable", { params: { domain: site } }),
    onSuccess: (result, site) => {
      toast.success(`Disabled ${site}`, { description: "The web server reloaded without it." });
      refresh(result.site);
    },
    onError: (error, site) => {
      reportActionError(`Could not disable ${site}`, error);
      refresh(site);
    },
  });

  const reload = useMutation({
    mutationFn: () => request("post", "/api/sites/reload"),
    onSuccess: (result) => {
      toast.success(`Reloaded ${result.webserver}`, { description: "Its configuration test passed first." });
    },
    onError: (error) => {
      reportActionError("The web server was not reloaded", error);
    },
  });

  return { enable, disable, reload, refresh };
}

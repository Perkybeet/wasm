import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import { monitorKeys } from "../../api/queries/monitor";
import { reportActionError } from "../apps/useAppActions";
import { toast } from "../../components/ui/toast";

type ServiceVerb = "install" | "uninstall" | "enable" | "disable" | "start" | "stop";

const WORDS: Record<ServiceVerb, string> = {
  install: "Installed",
  uninstall: "Removed",
  enable: "Enabled",
  disable: "Disabled",
  start: "Started",
  stop: "Stopped",
};

function callVerb(verb: ServiceVerb) {
  switch (verb) {
    case "install":
      return request("post", "/api/monitor/install");
    case "uninstall":
      return request("post", "/api/monitor/uninstall");
    case "enable":
      return request("post", "/api/monitor/enable");
    case "disable":
      return request("post", "/api/monitor/disable");
    case "start":
      return request("post", "/api/monitor/start");
    case "stop":
      return request("post", "/api/monitor/stop");
  }
}

/** The resource monitor's own actions: its systemd unit, its observations, a test email. */
export function useMonitorActions() {
  const queryClient = useQueryClient();

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: monitorKeys.status });
  };

  const useVerb = (verb: ServiceVerb) =>
    useMutation({
      mutationFn: () => callVerb(verb),
      onSuccess: () => {
        toast.success(`${WORDS[verb]} the resource monitor`);
        refresh();
      },
      onError: (error) => {
        reportActionError(`Could not ${verb} the resource monitor`, error);
        refresh();
      },
    });

  const install = useVerb("install");
  const uninstall = useVerb("uninstall");
  const enable = useVerb("enable");
  const disable = useVerb("disable");
  const start = useVerb("start");
  const stop = useVerb("stop");

  const testEmail = useMutation({
    mutationFn: () => request("post", "/api/monitor/test-email"),
    onSuccess: () => {
      toast.success("Test email sent");
    },
    onError: (error) => {
      reportActionError("The test email could not be sent", error);
    },
  });

  const acknowledge = useMutation({
    mutationFn: (id: number) => request("post", "/api/monitor/observations/{observation_id}/acknowledge", { params: { observation_id: id } }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: monitorKeys.all });
    },
    onError: (error) => {
      reportActionError("Could not acknowledge the observation", error);
    },
  });

  return { install, uninstall, enable, disable, start, stop, testEmail, acknowledge };
}

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import { serviceKeys } from "../../api/queries/services";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";

type Verb = "start" | "stop" | "restart" | "enable" | "disable";

const WORDS: Record<Verb, { past: string; noun: string }> = {
  start: { past: "Started", noun: "Start" },
  stop: { past: "Stopped", noun: "Stop" },
  restart: { past: "Restarted", noun: "Restart" },
  enable: { past: "Enabled", noun: "Enable" },
  disable: { past: "Disabled", noun: "Disable" },
};

function callVerb(name: string, verb: Verb) {
  const params = { params: { name } };
  switch (verb) {
    case "start":
      return request("post", "/api/services/{name}/start", params);
    case "stop":
      return request("post", "/api/services/{name}/stop", params);
    case "restart":
      return request("post", "/api/services/{name}/restart", params);
    case "enable":
      return request("post", "/api/services/{name}/enable", params);
    case "disable":
      return request("post", "/api/services/{name}/disable", params);
  }
}

/**
 * The actions on one systemd unit: start, stop, restart, enable and disable run and report
 * immediately (the endpoints are synchronous systemctl calls, not queued jobs); deleting and
 * saving the unit file need "Confirm it's you", which the API client asks for on its own.
 */
export function useServiceActions(name: string) {
  const queryClient = useQueryClient();

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: serviceKeys.detail(name) });
    void queryClient.invalidateQueries({ queryKey: serviceKeys.all });
  };

  const useVerb = (verb: Verb) =>
    useMutation({
      mutationFn: () => callVerb(name, verb),
      onSuccess: () => {
        toast.success(`${WORDS[verb].past} ${name}`);
        refresh();
      },
      onError: (error) => {
        reportActionError(`${WORDS[verb].noun} of ${name} failed`, error);
        refresh();
      },
    });

  const start = useVerb("start");
  const stop = useVerb("stop");
  const restart = useVerb("restart");
  const enable = useVerb("enable");
  const disable = useVerb("disable");

  const updateConfig = useMutation({
    mutationFn: (config: string) => request("put", "/api/services/{name}/config", { params: { name }, body: { config } }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: serviceKeys.config(name) });
      toast.success(`Saved the unit file for ${name}`, { description: "Restart the service to apply the change." });
    },
  });

  // Silent on purpose: its result feeds the unit editor's own pass/fail block, and a save that
  // goes on to succeed already toasts through updateConfig above - a second toast here would
  // just repeat it.
  const verifyUnit = useMutation({
    mutationFn: (content: string) => request("post", "/api/services/verify", { body: { content } }),
  });

  const remove = useMutation({
    mutationFn: () => request("delete", "/api/services/{name}", { params: { name } }),
    onSuccess: () => {
      toast.success(`Deleted ${name}`);
      void queryClient.invalidateQueries({ queryKey: serviceKeys.all });
    },
  });

  return { start, stop, restart, enable, disable, updateConfig, verifyUnit, remove };
}

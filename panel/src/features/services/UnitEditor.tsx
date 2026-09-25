import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { serviceConfigQuery } from "../../api/queries/services";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { Textarea } from "../../components/ui/Textarea";
import { useServiceActions } from "./useServiceActions";

/**
 * The unit file, read and written verbatim. Saving needs "Confirm it's you" (the API client
 * asks for elevation on its own); once it answers, the editor is refreshed from the same
 * `GET .../config` a plain read uses, so what is on screen after a save is what the manager
 * actually wrote, not merely what was typed - `ServiceManager.update_config` can, for
 * instance, restamp the WASM ownership marker.
 */
export function UnitEditor({ name }: { name: string }) {
  const config = useQuery(serviceConfigQuery(name));
  const { updateConfig } = useServiceActions(name);
  const [value, setValue] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Seeds the editor from the remote content once it first arrives, and again whenever a save
  // completes and refetches it (that second seed happens explicitly, in onSuccess below).
  // Setting state conditionally during render - not inside an effect - is what keeps this a
  // single render instead of a fetch-then-render-then-effect-then-render cascade.
  if (value === null && config.data !== undefined) {
    setValue(config.data.config);
  }

  const remote = config.data?.config ?? null;
  const dirty = value !== null && remote !== null && value !== remote;

  const save = (): void => {
    if (value === null) return;
    updateConfig.mutate(value, {
      onSuccess: () => {
        void config.refetch().then((fresh) => {
          if (fresh.data) setValue(fresh.data.config);
          setSavedAt(Date.now());
        });
      },
    });
  };

  return (
    <Section
      title="Unit file"
      description="The raw systemd unit. Saving requires confirming it's you, and restarting the service applies the change."
    >
      {config.isError && config.data === undefined ? (
        <ErrorBlock error={config.error} title="Could not load the unit file" onRetry={() => void config.refetch()} retrying={config.isRefetching} />
      ) : value === null ? (
        <Skeleton className="h-56 w-full rounded-card" />
      ) : (
        <div className="flex flex-col gap-2">
          <Textarea
            aria-label={`Unit file for ${name}`}
            mono
            rows={16}
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
              setSavedAt(null);
            }}
            spellCheck={false}
            className="min-h-56"
          />
          <div className="flex items-center justify-between gap-3">
            <p role="status" className="text-12 text-fg-muted">
              {updateConfig.isPending
                ? "Saving..."
                : savedAt !== null
                  ? "Saved. This is the unit file as it was written."
                  : dirty
                    ? "Unsaved changes."
                    : ""}
            </p>
            <div className="flex items-center gap-2">
              {dirty ? (
                <Button
                  onClick={() => {
                    setValue(remote);
                    setSavedAt(null);
                  }}
                >
                  Revert
                </Button>
              ) : null}
              <Button variant="primary" disabled={!dirty} loading={updateConfig.isPending} onClick={save}>
                Save unit file
              </Button>
            </div>
          </div>
          {updateConfig.isError ? <ErrorBlock live compact error={updateConfig.error} title="The unit file was not saved" /> : null}
        </div>
      )}
    </Section>
  );
}

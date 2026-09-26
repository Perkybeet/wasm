import { useQuery } from "@tanstack/react-query";
import { CircleAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { ElevationCancelledError } from "../../api/client";
import { serviceConfigQuery } from "../../api/queries/services";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { SystemOutput } from "../../components/ui/SystemOutput";
import { Textarea } from "../../components/ui/Textarea";
import { useServiceActions } from "./useServiceActions";

type Outcome = { kind: "rejected"; output: string } | { kind: "saved"; output: string } | null;

function VerifyRejected({ output }: { output: string }) {
  return (
    <div role="alert" className="flex flex-col gap-2 rounded-card border border-fail/30 bg-fail-soft/50 p-3">
      <div className="flex items-start gap-2">
        <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-fail" />
        <p className="text-13 text-fg">Nothing was saved: systemd-analyze rejected the unit. Fix what it names and save again.</p>
      </div>
      <SystemOutput label="What systemd-analyze said" maxHeight="max-h-40">
        {output}
      </SystemOutput>
    </div>
  );
}

function VerifyPassed({ output }: { output: string }) {
  const clean = output.trim() === "";
  return (
    <div role="status" className="flex flex-col gap-2 rounded-card border border-ok/30 bg-ok-soft/40 p-3">
      <div className="flex items-start gap-2">
        <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-ok" />
        <p className="text-13 text-fg">
          {clean ? "Saved. systemd-analyze found no problems." : "Saved. systemd-analyze passed, with this output:"}
        </p>
      </div>
      {clean ? null : (
        <SystemOutput label="What systemd-analyze said" maxHeight="max-h-32">
          {output}
        </SystemOutput>
      )}
    </div>
  );
}

/**
 * The unit file, read and written verbatim. Saving checks the candidate with `POST
 * /api/services/verify` (systemd-analyze) first, exactly the way the site config editor
 * blocks a save on a failing `nginx -t`: a refusal is shown in systemd's own words and
 * nothing is written. A pass goes on to save behind "Confirm it's you" (the API client asks
 * for elevation on its own); once it answers, the editor is refreshed from the same `GET
 * .../config` a plain read uses, so what is on screen after a save is what the manager
 * actually wrote, not merely what was typed - `ServiceManager.update_config` can, for
 * instance, restamp the WASM ownership marker.
 */
export function UnitEditor({ name }: { name: string }) {
  const config = useQuery(serviceConfigQuery(name));
  const { updateConfig, verifyUnit } = useServiceActions(name);
  const [value, setValue] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome>(null);

  // Seeds the editor from the remote content once it first arrives, and again whenever a save
  // completes and refetches it (that second seed happens explicitly, in onSuccess below).
  // Setting state conditionally during render - not inside an effect - is what keeps this a
  // single render instead of a fetch-then-render-then-effect-then-render cascade.
  if (value === null && config.data !== undefined) {
    setValue(config.data.config);
  }

  const remote = config.data?.config ?? null;
  const dirty = value !== null && remote !== null && value !== remote;
  const checking = verifyUnit.isPending;
  const saving = updateConfig.isPending;

  const save = (): void => {
    if (value === null) return;
    setOutcome(null);
    verifyUnit.mutate(value, {
      onSuccess: (result) => {
        if (!result.success) {
          setOutcome({ kind: "rejected", output: result.output });
          return;
        }
        updateConfig.mutate(value, {
          onSuccess: () => {
            void config.refetch().then((fresh) => {
              if (fresh.data) setValue(fresh.data.config);
            });
            setOutcome({ kind: "saved", output: result.output });
          },
        });
      },
    });
  };

  const onChange = (next: string): void => {
    setValue(next);
    setOutcome(null);
  };

  return (
    <Section
      title="Unit file"
      description="The raw systemd unit. Saving tests it with systemd-analyze first and requires confirming it's you; restarting the service applies the change."
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
            onChange={(event) => onChange(event.target.value)}
            disabled={checking || saving}
            spellCheck={false}
            className="min-h-56"
          />
          <div className="flex items-center justify-between gap-3">
            <p role="status" className="text-12 text-fg-muted">
              {checking ? "Checking with systemd-analyze..." : saving ? "Saving..." : outcome?.kind === "saved" ? "" : dirty ? "Unsaved changes." : ""}
            </p>
            <div className="flex items-center gap-2">
              {dirty ? (
                <Button
                  disabled={checking || saving}
                  onClick={() => {
                    setValue(remote);
                    setOutcome(null);
                  }}
                >
                  Revert
                </Button>
              ) : null}
              <Button variant="primary" disabled={!dirty} loading={checking || saving} onClick={save}>
                Save unit file
              </Button>
            </div>
          </div>
          {outcome?.kind === "rejected" ? (
            <VerifyRejected output={outcome.output} />
          ) : outcome?.kind === "saved" ? (
            <VerifyPassed output={outcome.output} />
          ) : verifyUnit.isError ? (
            <ErrorBlock live compact error={verifyUnit.error} title="The unit file could not be checked" />
          ) : updateConfig.isError && updateConfig.error instanceof ElevationCancelledError ? (
            <p role="status" className="text-13 text-fg-muted">
              {updateConfig.error.detail}
            </p>
          ) : updateConfig.isError ? (
            <ErrorBlock live compact error={updateConfig.error} title="The unit file was not saved" />
          ) : null}
        </div>
      )}
    </Section>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, RotateCw } from "lucide-react";
import { useState } from "react";
import type { SyntheticEvent } from "react";

import { isApiError, request } from "../../../api/client";
import type { ResponseOf } from "../../../api/client";
import { appKeys } from "../../../api/queries/apps";
import type { App } from "../../../api/queries/apps";
import { systemInfoQuery } from "../../../api/queries/system";
import { announce } from "../../../app/Announcer";
import { CommandHint } from "../../../components/page/CommandHint";
import { ErrorBlock } from "../../../components/page/QueryState";
import { Section } from "../../../components/page/Section";
import { appStatus } from "../../../components/page/status";
import { Button } from "../../../components/ui/Button";
import { Checkbox } from "../../../components/ui/Checkbox";
import { Field } from "../../../components/ui/Field";
import { Input } from "../../../components/ui/Input";
import { toast } from "../../../components/ui/toast";
import { ElevationCancelledError } from "../../../api/errors";
import { reportActionError, useAppActions } from "../../apps/useAppActions";
import { useConfirmItsYou } from "../useDeleteApp";
import { changed, directives, draftOf, parseLimits } from "./limits";
import type { LimitsDraft, LimitsErrors } from "./limits";
import { PANEL } from "./panel";

type LimitsResult = ResponseOf<"/api/apps/{domain}/limits", "patch">;

/** The request's field names, for a 422 whose `fields` name them. */
const FIELD_OF: Readonly<Record<string, keyof LimitsDraft>> = {
  memory_max_mb: "memory",
  cpu_quota_percent: "cpu",
  tasks_max: "tasks",
};

function sameDraft(a: LimitsDraft, b: LimitsDraft): boolean {
  return a.memory === b.memory && a.cpu === b.cpu && a.tasks === b.tasks;
}

function Saved({ result, domain }: { result: LimitsResult; domain: string }) {
  const { restart } = useAppActions(domain);
  const units = result.units.join(", ");
  return (
    <div role="status" className="flex flex-col gap-2 rounded-control border border-ok/40 bg-ok-soft px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
      <p className="flex items-start gap-2 text-13 text-fg">
        <CircleCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-ok" />
        <span>
          {result.restarted
            ? `Saved. ${units || "The unit"} restarted under the new limits.`
            : `Saved. ${units || "The unit"} was rewritten; the running process keeps its old limits until it restarts.`}
        </span>
      </p>
      {result.restart_required ? (
        <Button size="sm" icon={<RotateCw aria-hidden="true" />} loading={restart.isPending} onClick={() => restart.mutate()} className="self-start sm:self-auto">
          Restart now
        </Button>
      ) : null}
    </div>
  );
}

/**
 * The memory, CPU and task limits systemd enforces on the app's unit. An empty field is no
 * limit. The bounds are checked here for a quick answer and again, authoritatively, by the
 * backend, whose refusal is shown verbatim.
 */
export function LimitsSection({ app }: { app: App }) {
  const domain = app.domain;
  const queryClient = useQueryClient();
  const confirmItsYou = useConfirmItsYou();
  const staticSite = appStatus(app.status).state === "static";
  const info = useQuery({ ...systemInfoQuery(), enabled: !staticSite, staleTime: 10 * 60_000, refetchOnWindowFocus: false });
  const cores = info.data?.cpu.cores ?? null;

  const current = draftOf(app);
  const [baseline, setBaseline] = useState(current);
  const [draft, setDraft] = useState(current);
  const [restart, setRestart] = useState(false);
  const [touched, setTouched] = useState<Partial<Record<keyof LimitsDraft, boolean>>>({});
  const [submitted, setSubmitted] = useState(false);
  const [saved, setSaved] = useState<LimitsResult | null>(null);

  // The app changed underneath an untouched form (saved here, or from a terminal): follow it.
  if (!sameDraft(current, baseline)) {
    setBaseline(current);
    if (sameDraft(draft, baseline)) setDraft(current);
  }

  const parsed = parseLimits(draft, cores);
  const dirty = changed(draft, current);

  const save = useMutation({
    mutationFn: () =>
      request("patch", "/api/apps/{domain}/limits", {
        params: { domain },
        body: { ...parsed.values, restart },
      }),
    onSuccess: (result) => {
      setSaved(result);
      setSubmitted(false);
      setTouched({});
      setRestart(false);
      queryClient.setQueryData<App>(appKeys.detail(domain), (known) =>
        known
          ? {
              ...known,
              memory_max_mb: result.memory_max_mb ?? null,
              cpu_quota_percent: result.cpu_quota_percent ?? null,
              tasks_max: result.tasks_max ?? null,
            }
          : known,
      );
      void queryClient.invalidateQueries({ queryKey: appKeys.detail(domain) });
      void queryClient.invalidateQueries({ queryKey: appKeys.list, exact: true });
      const said = `Saved the limits of ${domain}`;
      announce(said);
      toast.success(said);
    },
  });

  const serverFields: LimitsErrors = {};
  if (isApiError(save.error) && save.error.fields) {
    for (const [name, message] of Object.entries(save.error.fields)) {
      const field = FIELD_OF[name];
      if (field) serverFields[field] = message;
    }
  }
  const errorOf = (field: keyof LimitsDraft): string | undefined =>
    (submitted || touched[field] ? parsed.errors[field] : undefined) ?? serverFields[field];

  const edit = (field: keyof LimitsDraft) => (value: string) => {
    setDraft((previous) => ({ ...previous, [field]: value }));
    setSaved(null);
    if (save.isError) save.reset();
  };
  const blur = (field: keyof LimitsDraft) => () => {
    setTouched((previous) => ({ ...previous, [field]: true }));
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setSubmitted(true);
    if (Object.keys(parsed.errors).length > 0 || !dirty || save.isPending) return;
    // Sudo mode is asked for up front rather than on the refusal, so the save is one request.
    confirmItsYou().then(
      () => {
        save.mutate();
      },
      (error: unknown) => {
        if (!(error instanceof ElevationCancelledError)) reportActionError(`The limits of ${domain} were not saved`, error);
      },
    );
  };

  const now = directives(app);

  if (staticSite) {
    return (
      <Section title="Resource limits">
        <p className={`${PANEL} px-4 py-4 text-13 text-fg-muted`}>
          A static site is served by the web server: there is no process of its own to limit.
        </p>
      </Section>
    );
  }

  return (
    <Section title="Resource limits" description="What systemd lets the app's unit use. An empty field is no limit.">
      <form onSubmit={submit} noValidate className={`${PANEL} flex flex-col gap-5 px-4 py-4 sm:px-5`}>
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Memory" description="MemoryMax. At least 64 MB." error={errorOf("memory")}>
            <Input
              mono
              inputMode="numeric"
              autoComplete="off"
              placeholder="No limit"
              suffix="MB"
              value={draft.memory}
              onValueChange={edit("memory")}
              onBlur={blur("memory")}
            />
          </Field>
          <Field
            label="CPU"
            description={
              cores === null
                ? "CPUQuota. 100 is one whole CPU, 200 two."
                : `CPUQuota. 100 is one whole CPU; up to ${String(100 * cores)} here.`
            }
            error={errorOf("cpu")}
          >
            <Input
              mono
              inputMode="numeric"
              autoComplete="off"
              placeholder="No limit"
              suffix="%"
              value={draft.cpu}
              onValueChange={edit("cpu")}
              onBlur={blur("cpu")}
            />
          </Field>
          <Field label="Tasks" description="TasksMax: processes and threads. At least 16." error={errorOf("tasks")}>
            <Input
              mono
              inputMode="numeric"
              autoComplete="off"
              placeholder="No limit"
              value={draft.tasks}
              onValueChange={edit("tasks")}
              onBlur={blur("tasks")}
            />
          </Field>
        </div>

        <Checkbox
          label="Restart now so the limits apply"
          description="Otherwise the running process keeps its current limits until the next restart or deploy."
          checked={restart}
          onCheckedChange={setRestart}
        />

        {save.isError && Object.keys(serverFields).length === 0 ? (
          <ErrorBlock live compact error={save.error} title="The limits were not saved" />
        ) : null}
        {saved !== null ? <Saved result={saved} domain={domain} /> : null}

        <div className="flex flex-col gap-3 border-t border-border pt-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-12 text-fg-muted">
            {"Now: "}
            {now.length > 0 ? (
              <span translate="no" className="mono text-fg">
                {now.join(" ")}
              </span>
            ) : (
              "no limits"
            )}
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              disabled={!dirty || save.isPending}
              onClick={() => {
                setDraft(current);
                setTouched({});
                setSubmitted(false);
                save.reset();
              }}
            >
              Discard changes
            </Button>
            <Button type="submit" variant="primary" disabled={!dirty} loading={save.isPending}>
              Save limits
            </Button>
          </div>
        </div>
      </form>
      <CommandHint command={`wasm app limits ${domain} --memory 512M --cpu 50% --tasks 256`} label="From a terminal" />
    </Section>
  );
}

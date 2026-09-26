import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleCheck } from "lucide-react";
import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { isApiError, request } from "../../../api/client";
import type { ResponseOf } from "../../../api/client";
import { ElevationCancelledError } from "../../../api/errors";
import { appKeys } from "../../../api/queries/apps";
import type { App } from "../../../api/queries/apps";
import { CommandHint } from "../../../components/page/CommandHint";
import { ErrorBlock } from "../../../components/page/QueryState";
import { Button } from "../../../components/ui/Button";
import { Field } from "../../../components/ui/Field";
import { Input } from "../../../components/ui/Input";
import { toast } from "../../../components/ui/toast";
import { reportActionError } from "../../apps/useAppActions";
import { useConfirmItsYou } from "../useDeleteApp";
import { HEALTH_DEFAULTS, effectiveHealth, healthDraftOf, healthFieldOf, parseHealth, sameHealth } from "./healthCheck";
import type { HealthDraft, HealthErrors } from "./healthCheck";
import { PANEL } from "./panel";

type HealthResult = ResponseOf<"/api/apps/{domain}/health", "patch">;

const EMPTY: HealthDraft = { path: "", expect: "", timeout: "" };

/** What the gate asks now, each value marked when it is the default. */
function Effective({ app }: { app: App }) {
  const now = effectiveHealth(app);
  const mark = (isDefault: boolean) => (isDefault ? <span className="text-fg-faint"> (default)</span> : null);
  return (
    <p className="text-12 text-pretty text-fg-muted">
      {"Now: "}
      <span translate="no" className="mono text-fg">{`GET ${now.path}`}</span>
      {mark(now.defaults.path)}
      {", passes on "}
      <span className="text-fg">{now.expect}</span>
      {mark(now.defaults.expect)}
      {", within "}
      <span className="mono text-fg">{`${String(now.timeout)}s`}</span>
      {mark(now.defaults.timeout)}
      .
    </p>
  );
}

/**
 * What the health gate asks of the app before a release (or an in-place update) may serve: the
 * path it requests on 127.0.0.1, the statuses that pass and how long it waits. An empty field
 * is the default. The rules are checked here for a quick answer and again, authoritatively,
 * where the settings are stored; a refusal lands on the field it is about. Nothing restarts:
 * the next activation uses them.
 */
export function HealthCheckForm({ app }: { app: App }) {
  const domain = app.domain;
  const headingId = useId();
  const queryClient = useQueryClient();
  const confirmItsYou = useConfirmItsYou();

  const current = healthDraftOf(app);
  const [baseline, setBaseline] = useState(current);
  const [draft, setDraft] = useState(current);
  const [touched, setTouched] = useState<Partial<Record<keyof HealthDraft, boolean>>>({});
  const [submitted, setSubmitted] = useState(false);
  const [saved, setSaved] = useState<HealthResult | null>(null);

  // The app changed underneath an untouched form (saved here, or from a terminal): follow it.
  if (!sameHealth(current, baseline)) {
    setBaseline(current);
    if (sameHealth(draft, baseline)) setDraft(current);
  }

  const parsed = parseHealth(draft);
  const dirty = !sameHealth(draft, current);

  const save = useMutation({
    mutationFn: () => request("patch", "/api/apps/{domain}/health", { params: { domain }, body: parsed.values }),
    onSuccess: (result) => {
      setSaved(result);
      setSubmitted(false);
      setTouched({});
      queryClient.setQueryData<App>(appKeys.detail(domain), (known) =>
        known ? { ...known, health_path: result.path ?? null, health_expect: result.expect ?? null, health_timeout: result.timeout ?? null } : known,
      );
      void queryClient.invalidateQueries({ queryKey: appKeys.detail(domain) });
      // The toast is announced; saying it again would read it twice.
      toast.success(`Saved the health check of ${domain}`);
    },
  });

  const serverFields: HealthErrors = {};
  let unplaced = false;
  if (save.isError && isApiError(save.error)) {
    if (save.error.fields) {
      for (const [name, message] of Object.entries(save.error.fields)) {
        if (name === "path" || name === "expect" || name === "timeout") serverFields[name] = message;
      }
    } else {
      const field = healthFieldOf(save.error.detail);
      if (field !== null) serverFields[field] = save.error.hint ? `${save.error.detail}. ${save.error.hint}` : save.error.detail;
    }
    unplaced = Object.keys(serverFields).length === 0;
  } else if (save.isError) {
    unplaced = true;
  }
  const errorOf = (field: keyof HealthDraft): string | undefined =>
    (submitted || touched[field] ? parsed.errors[field] : undefined) ?? serverFields[field];

  const edit = (field: keyof HealthDraft) => (value: string) => {
    setDraft((previous) => ({ ...previous, [field]: value }));
    setSaved(null);
    if (save.isError) save.reset();
  };
  const blur = (field: keyof HealthDraft) => () => {
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
        if (!(error instanceof ElevationCancelledError)) reportActionError(`The health check of ${domain} was not saved`, error);
      },
    );
  };

  return (
    <section aria-labelledby={headingId} className={`${PANEL} flex flex-col gap-4 px-4 py-4 sm:px-5`}>
      <header className="flex flex-col gap-1">
        <h3 id={headingId} className="text-14 font-medium text-fg">
          Health check
        </h3>
        <p className="text-13 text-pretty text-fg-muted">
          What a new version must answer before it serves. An empty field is the default. Nothing restarts: the next deploy, update or
          rollback uses it.
        </p>
      </header>
      <form onSubmit={submit} noValidate className="flex flex-col gap-4">
        <Field label="Path" description={`Requested on 127.0.0.1 at the app's port. Default ${HEALTH_DEFAULTS.path}.`} error={errorOf("path")}>
          <Input
            mono
            autoComplete="off"
            autoCapitalize="off"
            spellCheck={false}
            placeholder={HEALTH_DEFAULTS.path}
            value={draft.path}
            onValueChange={edit("path")}
            onBlur={blur("path")}
          />
        </Field>
        <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_9rem]">
          <Field
            label="Accepted statuses"
            description="Such as 200-399 or 200,204. A redirect is not followed. Default: any status below 500."
            error={errorOf("expect")}
          >
            <Input
              mono
              autoComplete="off"
              placeholder="Any below 500"
              value={draft.expect}
              onValueChange={edit("expect")}
              onBlur={blur("expect")}
            />
          </Field>
          <Field label="Timeout" description={`5 to 600. Default ${String(HEALTH_DEFAULTS.timeout)}.`} error={errorOf("timeout")}>
            <Input
              mono
              inputMode="numeric"
              autoComplete="off"
              placeholder={String(HEALTH_DEFAULTS.timeout)}
              suffix="s"
              value={draft.timeout}
              onValueChange={edit("timeout")}
              onBlur={blur("timeout")}
            />
          </Field>
        </div>

        {unplaced ? <ErrorBlock live compact error={save.error} title="The health check was not saved" /> : null}
        {saved !== null ? (
          <p role="status" className="flex items-start gap-2 rounded-control border border-ok/40 bg-ok-soft px-3 py-2.5 text-13 text-fg">
            <CircleCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-ok" />
            <span>
              {"Saved. The next activation requests "}
              <span translate="no" className="mono text-12">
                {saved.effective_path}
              </span>
              {` and passes on ${saved.effective_expect} within ${String(saved.effective_timeout)}s.`}
            </span>
          </p>
        ) : null}

        <div className="flex flex-col gap-3 border-t border-border pt-4 sm:flex-row sm:items-center sm:justify-between">
          <Effective app={app} />
          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="ghost"
              disabled={save.isPending || sameHealth(draft, EMPTY)}
              onClick={() => {
                setDraft(EMPTY);
                setSaved(null);
                save.reset();
              }}
            >
              Use defaults
            </Button>
            <Button type="submit" variant="primary" disabled={!dirty} loading={save.isPending}>
              Save
            </Button>
          </div>
        </div>
      </form>
      <CommandHint command={`wasm app health ${domain} --path /healthz --expect 200-299 --timeout 60`} label="From a terminal" />
    </section>
  );
}

/** A static site's check is its files; there is nothing to configure. */
export function StaticHealthNote() {
  return (
    <div className={`${PANEL} flex flex-col gap-1 px-4 py-4`}>
      <h3 className="text-14 font-medium text-fg">Health check</h3>
      <p className="text-13 text-pretty text-fg-muted">
        A static site is served as files by the web server, with no process of its own to ask. Its check is that the directory it
        serves has an index.html, so there is nothing to configure here.
      </p>
    </div>
  );
}

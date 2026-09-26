import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Send, TriangleAlert } from "lucide-react";
import { useId, useState } from "react";
import type { ReactNode, SyntheticEvent } from "react";

import { configKeys, configQuery, patchConfig, testNotificationChannel } from "../../api/queries/config";
import type { ConsoleConfig, NotificationTestResult } from "../../api/queries/config";
import { useDocumentTitle } from "../../app/documentTitle";
import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock, QueryState } from "../../components/page/QueryState";
import { Sections } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Field } from "../../components/ui/Field";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusGlyph, StatusPill } from "../../components/ui/StatusPill";
import { SystemOutput } from "../../components/ui/SystemOutput";
import { Switch } from "../../components/ui/Switch";
import { Textarea } from "../../components/ui/Textarea";
import { toast } from "../../components/ui/toast";
import { cx } from "../../lib/cx";
import { reportActionError } from "../apps/useAppActions";
import { splitErrors } from "./formErrors";
import { CHANNELS, EVENTS, REDACTED, channelValue, isChannelConfigured, parseHostList, readNotificationSettings } from "./notifications";
import type { ChannelField, ChannelSpec, NotificationSettings as Settings } from "./notifications";
import { configSetCommand } from "./shell";
import { SettingsFormCard, SettingsSection } from "./SettingsForm";
import { useSettingsForm } from "./useSettingsForm";

function useRefreshConfig() {
  const queryClient = useQueryClient();
  return (): Promise<void> => queryClient.invalidateQueries({ queryKey: configKeys.all });
}

const SURFACE = "rounded-card border border-border bg-surface shadow-raised";

// ---------------------------------------------------------------------------------------
// The master switch

function DeliverySection({ query }: { query: ConfigQuery }) {
  const refresh = useRefreshConfig();
  const toggle = useMutation({
    mutationFn: (next: boolean) => patchConfig("notifications.enabled", next),
    onSuccess: async (_, next) => {
      await refresh();
      toast.success(next ? "Turned notifications on" : "Turned notifications off");
    },
    onError: (error, next) => {
      reportActionError(next ? "Could not turn notifications on" : "Could not turn notifications off", error);
    },
  });
  const enabled = query.data === undefined ? undefined : readNotificationSettings(query.data.config).enabled;
  // While the write is in flight (and "Confirm it's you" is open) the switch shows where it is going.
  const checked = toggle.isPending ? toggle.variables : (enabled ?? false);
  return (
    <SettingsSection
      title="Delivery"
      description="The one switch for every channel. Test messages are sent whether it is on or off, so a channel can be tried first."
      commands={[configSetCommand("notifications.enabled", !checked)]}
    >
      <WithSettings query={query} rows={2}>
        {() => (
          <div className={cx(SURFACE, "px-5 py-4")}>
            <Switch
              label="Send notifications"
              description={checked ? "Events turned on below go to every channel with a destination." : "Nothing is sent."}
              checked={checked}
              disabled={toggle.isPending}
              onCheckedChange={(next) => {
                toggle.mutate(next);
              }}
            />
          </div>
        )}
      </WithSettings>
    </SettingsSection>
  );
}

// ---------------------------------------------------------------------------------------
// Channels

function TestOutcome({ result, error }: { result: NotificationTestResult | undefined; error: unknown }) {
  if (error !== null && error !== undefined) {
    return <ErrorBlock compact error={error} title="The test could not be sent" />;
  }
  if (result === undefined) return null;
  if (result.ok) {
    return (
      <p className="flex items-center gap-2 text-13 text-fg">
        <StatusGlyph state="running" className="text-ok" />
        {result.detail}
      </p>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <p className="flex items-center gap-2 text-13 font-medium text-fg">
        <StatusGlyph state="failed" className="text-fail" />
        The test failed. The server said:
      </p>
      <SystemOutput label="What the server said" maxHeight="max-h-40" className="rounded-control border border-border bg-bg-sunken px-3 py-2">
        {result.detail}
      </SystemOutput>
    </div>
  );
}

/** A write-only field: what is typed can be shown, what is stored never comes back. */
function SecretInput({
  field,
  value,
  configured,
  onChange,
  disabled,
}: {
  field: ChannelField;
  value: string;
  /** Whether a value is already stored, so the placeholder says so instead of showing the format hint. */
  configured: boolean;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  const [shown, setShown] = useState(false);
  return (
    <Input
      mono
      type={shown ? "text" : "password"}
      autoComplete="off"
      autoCapitalize="off"
      spellCheck={false}
      placeholder={configured ? "Set - leave blank to keep it" : field.placeholder}
      value={value}
      disabled={disabled}
      onValueChange={(next: string) => {
        onChange(next);
      }}
      suffix={
        <IconButton
          size="sm"
          label={shown ? `Hide the ${field.label.toLowerCase()}` : `Show the ${field.label.toLowerCase()}`}
          icon={shown ? <EyeOff /> : <Eye />}
          pressed={shown}
          onClick={() => {
            setShown((current) => !current);
          }}
        />
      }
    />
  );
}

function useChannelTest(channel: string) {
  return useMutation({ mutationFn: () => testNotificationChannel(channel) });
}

/** Sending a test needs a destination to send to; a dirty form needs saving before it means anything. */
function TestButton({ test, disabled, reason }: { test: ReturnType<typeof useChannelTest>; disabled: boolean; reason?: string }) {
  const reasonId = useId();
  return (
    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
      {reason !== undefined ? (
        <span id={reasonId} className="text-12 text-fg-faint">
          {reason}
        </span>
      ) : null}
      <Button
        size="sm"
        icon={<Send aria-hidden="true" />}
        loading={test.isPending}
        disabled={disabled}
        {...(reason !== undefined ? { "aria-describedby": reasonId } : {})}
        onClick={() => {
          test.mutate();
        }}
      >
        Send test
      </Button>
    </span>
  );
}

/** A channel's name, whether it has a destination, what it does, and the actions that do not need its form. */
function ChannelHeader({
  id,
  label,
  description,
  configured,
  actions,
}: {
  id: string;
  label: string;
  description: string;
  configured: boolean;
  actions: ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div className="min-w-0 flex-1 basis-60">
        <div className="flex flex-wrap items-center gap-2">
          <h3 id={id} className="text-14 font-semibold text-fg">
            {label}
          </h3>
          <StatusPill
            state={configured ? "running" : "stopped"}
            label={configured ? "Configured" : "Not configured"}
            appearance="inline"
            size="sm"
          />
        </div>
        <p className="max-w-[60ch] text-13 text-fg-muted">{description}</p>
      </div>
      {/* Wraps on a phone: a reason, Remove and Send test do not fit one narrow row. */}
      <div className="flex max-w-full min-w-0 flex-wrap items-center gap-1">{actions}</div>
    </header>
  );
}

/** Save and Discard, only once something changed: a clean channel shows no dead buttons. */
function DirtyActions({ dirty, pending, onDiscard, note }: { dirty: boolean; pending: boolean; onDiscard: () => void; note?: string }) {
  if (!dirty && !pending) return null;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button type="submit" size="sm" variant="primary" loading={pending}>
        Save
      </Button>
      <Button size="sm" variant="ghost" disabled={pending} onClick={onDiscard}>
        Discard
      </Button>
      {note !== undefined ? <p className="text-12 text-fg-muted">{note}</p> : null}
    </div>
  );
}

/** One webhook-style channel: its destination, saved on its own, and a test. */
function HttpChannel({ spec, stored }: { spec: ChannelSpec; stored: Readonly<Record<string, string>> }) {
  const refresh = useRefreshConfig();
  const headingId = useId();
  const [draft, setDraft] = useState<Record<string, string>>({});
  const test = useChannelTest(spec.id);

  const secretKeys = spec.fields.filter((field) => field.secret).map((field) => field.key);
  const dirty = spec.fields.some((field) =>
    field.secret ? (draft[field.key] ?? "").trim() !== "" : field.key in draft && draft[field.key] !== stored[field.key],
  );
  const configured = isChannelConfigured(spec, stored);

  const save = useMutation({
    mutationFn: (cleared: ReadonlySet<string>) =>
      patchConfig(`notifications.channels.${spec.id}`, channelValue(spec, stored, draft, cleared)),
    onSuccess: async (_, cleared) => {
      setDraft({});
      test.reset();
      await refresh();
      toast.success(cleared.size > 0 ? `Removed the ${spec.label} destination` : `Saved the ${spec.label} destination`);
    },
  });
  const removing = save.isPending && save.variables.size > 0;
  const errors = splitErrors(save.error, spec.fields.map((field) => field.key));

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (dirty && !save.isPending) save.mutate(new Set());
  };

  const testReason = !configured ? "Add a destination to test it." : dirty ? "Save your changes first." : undefined;

  return (
    <article aria-labelledby={headingId} className="flex min-w-0 flex-col gap-3 px-5 py-4">
      <ChannelHeader
        id={headingId}
        label={spec.label}
        description={spec.description}
        configured={configured}
        actions={
          <>
            {/* Nothing to remove until a destination is saved. */}
            {configured || removing ? (
              <Button
                size="sm"
                variant="ghost"
                disabled={save.isPending && !removing}
                loading={removing}
                onClick={() => {
                  save.mutate(new Set(secretKeys));
                }}
              >
                Remove destination
              </Button>
            ) : null}
            <TestButton test={test} disabled={dirty || !configured} {...(testReason !== undefined ? { reason: testReason } : {})} />
          </>
        }
      />
      <form noValidate onSubmit={submit} className="flex min-w-0 flex-col gap-3">
        {errors.form !== null ? <ErrorBlock live compact error={errors.form} title={`Could not save ${spec.label}`} /> : null}
        <div className={cx("grid min-w-0 gap-3", spec.fields.length > 1 && "sm:grid-cols-2")}>
          {spec.fields.map((field) => {
            const value = draft[field.key] ?? stored[field.key] ?? "";
            const warning = field.secret ? null : (field.warn?.(value) ?? null);
            return (
              <Field key={field.key} label={field.label} description={field.description} error={errors.fields[field.key]}>
                {field.secret ? (
                  <SecretInput
                    field={field}
                    value={draft[field.key] ?? ""}
                    configured={stored[field.key] === REDACTED}
                    disabled={save.isPending}
                    onChange={(next) => {
                      setDraft((current) => ({ ...current, [field.key]: next }));
                    }}
                  />
                ) : (
                  <Input
                    mono
                    autoComplete="off"
                    spellCheck={false}
                    placeholder={field.placeholder}
                    value={value}
                    disabled={save.isPending}
                    onValueChange={(next: string) => {
                      setDraft((current) => ({ ...current, [field.key]: next }));
                    }}
                  />
                )}
                {warning !== null ? (
                  <p role="alert" className="flex items-start gap-1.5 text-13 text-warn">
                    <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                    <span>{warning}</span>
                  </p>
                ) : null}
              </Field>
            );
          })}
        </div>
        <DirtyActions
          dirty={dirty}
          pending={save.isPending && !removing}
          onDiscard={() => {
            setDraft({});
          }}
          note="A test goes to what is saved."
        />
      </form>
      <div role="status" className="min-w-0 empty:hidden">
        <TestOutcome result={test.data} error={test.error} />
      </div>
    </article>
  );
}

/** Email: on or off here; the SMTP account itself is the monitor's. */
function EmailChannel({ settings }: { settings: Settings }) {
  const refresh = useRefreshConfig();
  const headingId = useId();
  const test = useChannelTest("email");
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const value = enabled ?? settings.emailEnabled;
  const dirty = enabled !== null && enabled !== settings.emailEnabled;
  const save = useMutation({
    mutationFn: (next: boolean) => patchConfig("notifications.channels.email", { enabled: next }),
    onSuccess: async (_, next) => {
      await refresh();
      setEnabled(null);
      test.reset();
      toast.success(next ? "Turned email notifications on" : "Turned email notifications off");
    },
  });
  const { smtp } = settings;
  const configured = smtp.host !== "";
  const testReason = !configured ? "Set up the SMTP server to test it." : dirty ? "Save your changes first." : undefined;
  return (
    <article aria-labelledby={headingId} className="flex min-w-0 flex-col gap-3 px-5 py-4">
      <ChannelHeader
        id={headingId}
        label="Email"
        description="Sent through the monitor's SMTP account to its recipients."
        configured={configured}
        actions={<TestButton test={test} disabled={dirty || !configured} {...(testReason !== undefined ? { reason: testReason } : {})} />}
      />
      <form
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          if (dirty && !save.isPending) save.mutate(value);
        }}
        className="flex min-w-0 flex-col gap-3"
      >
        {save.isError ? <ErrorBlock live compact error={save.error} title="Could not save email" /> : null}
        <Checkbox
          label="Send notifications by email"
          checked={value}
          disabled={save.isPending}
          onCheckedChange={(next) => {
            setEnabled(next);
          }}
        />
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-1 rounded-control border border-border bg-bg-sunken px-3 py-2 text-13">
          <dt className="text-fg-muted">SMTP server</dt>
          <dd translate="no" className={cx("truncate", configured ? "mono text-12 text-fg" : "text-fg-faint")}>
            {configured ? `${smtp.host}${smtp.port === null ? "" : `:${String(smtp.port)}`}` : "Not set up"}
          </dd>
          <dt className="text-fg-muted">From</dt>
          <dd translate="no" className={cx("truncate", smtp.from ? "mono text-12 text-fg" : "text-fg-faint")}>
            {smtp.from || "Not set"}
          </dd>
          <dt className="text-fg-muted">Recipients</dt>
          <dd translate="no" className={cx("break-words", smtp.recipients.length > 0 ? "mono text-12 text-fg" : "text-fg-faint")}>
            {smtp.recipients.length > 0 ? smtp.recipients.join(", ") : "None"}
          </dd>
        </dl>
        {!configured ? <CommandHint label="Set it up from a terminal" command="wasm config set monitor.smtp.host smtp.example.com" /> : null}
        <DirtyActions
          dirty={dirty}
          pending={save.isPending}
          onDiscard={() => {
            setEnabled(null);
          }}
        />
      </form>
      <div role="status" className="min-w-0 empty:hidden">
        <TestOutcome result={test.data} error={test.error} />
      </div>
    </article>
  );
}

function ChannelsSection({ query }: { query: ConfigQuery }) {
  return (
    <SettingsSection
      title="Where alerts go"
      description="Every channel with a destination receives the events turned on below. Webhook URLs and the bot token are write-only: once saved they are never shown again, and a field left empty keeps what is stored. Send a test to find out what a channel does."
    >
      <WithSettings query={query} rows={6}>
        {(settings) => (
          <div className={cx(SURFACE, "flex min-w-0 flex-col divide-y divide-border")}>
            {CHANNELS.map((spec) =>
              spec.id === "email" ? (
                <EmailChannel key={spec.id} settings={settings} />
              ) : (
                <HttpChannel key={spec.id} spec={spec} stored={settings.channels[spec.id]} />
              ),
            )}
          </div>
        )}
      </WithSettings>
    </SettingsSection>
  );
}

// ---------------------------------------------------------------------------------------
// Events

const EVENT_NAMES = EVENTS.map((event) => event.kind);

function EventsSection({ query }: { query: ConfigQuery }) {
  const refresh = useRefreshConfig();
  const form = useSettingsForm<Record<string, boolean>>({
    server: query.data === undefined ? undefined : readNotificationSettings(query.data.config).events,
    names: EVENT_NAMES,
    save: async (values) => {
      await patchConfig("notifications.events", Object.fromEntries(EVENT_NAMES.map((kind) => [kind, values[kind] === true])));
      await refresh();
      toast.success("Saved the notification events");
    },
  });
  const values = form.values ?? {};
  return (
    <SettingsSection
      title="Events"
      description="Which events are sent. Each one goes to every channel with a destination; choosing per channel is not supported yet."
      commands={
        form.dirty
          ? form.changed.map((kind) => configSetCommand(`notifications.events.${kind}`, values[kind] === true))
          : ["wasm config get notifications.events"]
      }
    >
      <WithSettings query={query} rows={6}>
        {() => (
          <SettingsFormCard
            dirty={form.dirty}
            pending={form.pending}
            formError={form.formError}
            errorTitle="Could not save the events"
            onSubmit={form.submit}
            onDiscard={form.discard}
          >
            <fieldset className="flex flex-col gap-3.5">
              <legend className="sr-only">Events to send</legend>
              {EVENTS.map((event) => (
                <Checkbox
                  key={event.kind}
                  label={event.label}
                  description={event.unsent ? `${event.description} Not sent by this version of WASM yet.` : event.description}
                  checked={values[event.kind] === true}
                  onCheckedChange={(next) => {
                    form.set(event.kind, next);
                  }}
                />
              ))}
            </fieldset>
          </SettingsFormCard>
        )}
      </WithSettings>
    </SettingsSection>
  );
}

// ---------------------------------------------------------------------------------------
// Private destinations

function PrivateHostsSection({ query }: { query: ConfigQuery }) {
  const refresh = useRefreshConfig();
  const form = useSettingsForm({
    server:
      query.data === undefined
        ? undefined
        : { allow_private_hosts: readNotificationSettings(query.data.config).allowPrivateHosts.join("\n") },
    names: ["allow_private_hosts"],
    soleField: "allow_private_hosts",
    save: async ({ allow_private_hosts }) => {
      await patchConfig("notifications.allow_private_hosts", parseHostList(allow_private_hosts));
      await refresh();
      toast.success("Saved the private destinations");
    },
  });
  const typed = form.values?.allow_private_hosts ?? "";
  return (
    <SettingsSection
      title="Private destinations"
      description="A destination on this machine or a private network is refused, so a notification cannot be aimed at the console itself or a cloud metadata service. List a host here to allow it anyway."
      // `wasm config set` stores a list as one string, so reading is the only honest command.
      commands={["wasm config get notifications.allow_private_hosts"]}
    >
      <WithSettings query={query} rows={2}>
        {() => (
          <SettingsFormCard
            dirty={form.dirty}
            pending={form.pending}
            formError={form.formError}
            errorTitle="Could not save the private destinations"
            onSubmit={form.submit}
            onDiscard={form.discard}
          >
            <Field
              label="Allowed private hosts"
              optional
              description="One host name or address per line, exactly as it appears in the destination URL."
              error={form.fieldErrors.allow_private_hosts}
            >
              <Textarea
                mono
                rows={3}
                placeholder="10.0.0.12"
                value={typed}
                onChange={(event) => {
                  form.set("allow_private_hosts", event.target.value);
                }}
              />
            </Field>
          </SettingsFormCard>
        )}
      </WithSettings>
    </SettingsSection>
  );
}

type ConfigQuery = ReturnType<typeof useQuery<ConsoleConfig>>;

/** A section's content once the configuration is read; its skeleton or the failure until then. */
function WithSettings({ query, rows, children }: { query: ConfigQuery; rows: number; children: (settings: Settings) => ReactNode }) {
  return (
    <QueryState
      query={query}
      label="the notification settings"
      skeleton={
        <div className={cx(SURFACE, "flex flex-col gap-3 p-5")}>
          {Array.from({ length: rows }, (_, index) => (
            <Skeleton key={index} className="h-4 w-full max-w-sm" />
          ))}
        </div>
      }
    >
      {(data) => children(readNotificationSettings(data.config))}
    </QueryState>
  );
}

/** Settings > Notifications: the channels alerts go to, what is sent, and a test per channel. */
export function NotificationSettings() {
  useDocumentTitle("Notifications settings", 1);
  const query = useQuery(configQuery());
  return (
    <Sections>
      <DeliverySection query={query} />
      <ChannelsSection query={query} />
      <EventsSection query={query} />
      <PrivateHostsSection query={query} />
    </Sections>
  );
}

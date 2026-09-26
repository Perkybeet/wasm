import { useMutation, useQuery } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { patchConfig, saveSmtpSettings, smtpSettingsQuery } from "../../api/queries/config";
import { ErrorBlock } from "../../components/page/QueryState";
import { SegmentedControl } from "../../components/page/SegmentedControl";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Field } from "../../components/ui/Field";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusGlyph } from "../../components/ui/StatusPill";
import { toast } from "../../components/ui/toast";
import { cx } from "../../lib/cx";
import { ChannelHeader, DirtyActions, SecretInput, TestButton, TestOutcome, useChannelTest, useRefreshConfig } from "./channelParts";
import { splitConfigErrors } from "./formErrors";
import {
  SMTP_CONFIG_KEYS,
  SMTP_FIELDS,
  SMTP_SECURITY,
  looksLikeEmail,
  portForSecurity,
  refusedRecipients,
  smtpBody,
  smtpFormDirty,
  smtpFormFrom,
  splitAddresses,
} from "./notifications";
import type { SmtpField, SmtpForm, SmtpSecurity } from "./notifications";

const DESCRIPTION = "Sent through your SMTP server to the recipients below. The resource monitor's own reports use the same account.";

/**
 * The addresses email goes to: each one on its own row with a way to remove it, and a box that
 * adds one or several. The shape of each address is checked as it is added; the server still
 * rules on the list, and an address it refuses is marked where it stands.
 */
function RecipientsEditor({
  recipients,
  onChange,
  error,
  disabled,
}: {
  recipients: readonly string[];
  onChange: (next: readonly string[]) => void;
  /** The server's refusal of the list, verbatim. */
  error: string | undefined;
  disabled: boolean;
}) {
  const [typed, setTyped] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const legendId = useId();
  const refused = refusedRecipients(error, recipients);

  const add = (): void => {
    const addresses = splitAddresses(typed);
    if (addresses.length === 0) return;
    const invalid = addresses.filter((address) => !looksLikeEmail(address));
    if (invalid.length > 0) {
      setProblem(
        invalid.length === 1
          ? `${invalid[0] ?? ""} is not an email address. Use one such as ops@example.com.`
          : `${invalid.join(", ")} are not email addresses. Use addresses such as ops@example.com.`,
      );
      return;
    }
    const known = new Set(recipients.map((address) => address.toLowerCase()));
    const fresh = addresses.filter((address) => {
      const key = address.toLowerCase();
      if (known.has(key)) return false;
      known.add(key);
      return true;
    });
    onChange([...recipients, ...fresh]);
    setTyped("");
    setProblem(null);
  };

  return (
    <div role="group" aria-labelledby={legendId} className="flex min-w-0 flex-col gap-1.5">
      <span id={legendId} className="text-13 font-medium text-fg">
        Recipients
      </span>
      {recipients.length > 0 ? (
        <ul aria-label="Recipients" className="flex min-w-0 flex-col divide-y divide-border rounded-control border border-border bg-bg-sunken">
          {recipients.map((address) => {
            const bad = refused.has(address);
            return (
              <li key={address} className="flex min-h-9 min-w-0 items-center gap-2 py-1 pr-1 pl-3">
                {bad ? <StatusGlyph state="failed" size={10} className="text-fail" /> : null}
                <span translate="no" className={cx("mono min-w-0 flex-1 truncate text-12", bad ? "text-fail" : "text-fg")} title={address}>
                  {address}
                </span>
                {bad ? <span className="shrink-0 text-12 text-fail">Refused</span> : null}
                <IconButton
                  size="sm"
                  label={`Remove ${address}`}
                  icon={<X />}
                  disabled={disabled}
                  onClick={() => {
                    onChange(recipients.filter((other) => other !== address));
                  }}
                />
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="rounded-control border border-dashed border-border px-3 py-2 text-13 text-fg-muted">
          No recipients yet: nothing is sent by email until there is one.
        </p>
      )}
      <Field label="Add a recipient" error={problem ?? error} description="Several at once, separated by commas, are fine.">
        <div className="flex min-w-0 items-center gap-2">
          <Input
            mono
            type="email"
            inputMode="email"
            autoComplete="off"
            spellCheck={false}
            placeholder="ops@example.com"
            value={typed}
            disabled={disabled}
            className="flex-1"
            onValueChange={(next: string) => {
              setTyped(next);
              setProblem(null);
            }}
            onKeyDown={(event) => {
              // Enter adds the address instead of submitting the whole form half-edited.
              if (event.key === "Enter") {
                event.preventDefault();
                add();
              }
            }}
          />
          <Button size="md" icon={<Plus aria-hidden="true" />} disabled={disabled || typed.trim() === ""} onClick={add}>
            Add
          </Button>
        </div>
      </Field>
    </div>
  );
}

/**
 * The SMTP form's own shape while its settings load: every field's label, box and help line,
 * and the recipients' empty list. Skeleton's own line height would override a thinner one, so
 * a one-line help text is a fixed-height row holding a line.
 */
export function EmailFormSkeleton() {
  const helpLine = (width: string) => (
    <div className="flex h-4 items-center">
      <Skeleton className={width} />
    </div>
  );
  const field = (label: string, help: boolean) => (
    <div className="flex min-w-0 flex-col gap-1.5">
      <span className="text-13 font-medium text-fg">{label}</span>
      <Skeleton className="h-8" />
      {help ? helpLine("w-64 max-w-full") : null}
    </div>
  );
  return (
    <div aria-hidden="true" className="flex min-w-0 flex-col gap-3">
      <div className="flex h-5 items-center">
        <Skeleton className="w-48" />
      </div>
      <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,1fr)_8rem]">
        {field("SMTP server", false)}
        {field("Port", false)}
      </div>
      <div className="flex min-w-0 flex-col gap-1.5">
        <span className="text-13 font-medium text-fg">Encryption</span>
        <Skeleton className="h-[1.875rem] w-56" />
        {helpLine("w-80 max-w-full")}
      </div>
      <div className="grid min-w-0 gap-3 sm:grid-cols-2">
        {field("Username", true)}
        {field("Password", true)}
      </div>
      {field("From address", true)}
      <div className="flex min-w-0 flex-col gap-1.5">
        <span className="text-13 font-medium text-fg">Recipients</span>
        <Skeleton className="h-[2.375rem] rounded-control" />
        {field("Add a recipient", true)}
      </div>
    </div>
  );
}

/**
 * Email: on or off, and the SMTP account it goes through, as one form. The password is
 * write-only, like the webhook URLs: GET /api/config/smtp only says whether one is stored, and
 * a field left empty keeps it.
 */
export function EmailChannel({ enabledStored }: { enabledStored: boolean }) {
  const smtp = useQuery(smtpSettingsQuery());
  const refresh = useRefreshConfig();
  const headingId = useId();
  const test = useChannelTest("email");
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [draft, setDraft] = useState<Partial<SmtpForm>>({});
  // A field's error is hidden once it is edited: the message is about the value that was sent.
  const [edited, setEdited] = useState<ReadonlySet<SmtpField>>(new Set());

  const stored = smtp.data === undefined ? undefined : smtpFormFrom(smtp.data);
  const form: SmtpForm | undefined = stored === undefined ? undefined : { ...stored, ...draft };
  const enabledValue = enabled ?? enabledStored;
  const enabledDirty = enabled !== null && enabled !== enabledStored;
  const smtpDirty = form !== undefined && stored !== undefined && smtpFormDirty(form, stored);
  const dirty = enabledDirty || smtpDirty;

  const save = useMutation({
    mutationFn: async ({ values, turnOn }: { values: SmtpForm | null; turnOn: boolean | null }) => {
      // The account first: turning email on over an account the server refused would send nothing.
      if (values !== null) await saveSmtpSettings(smtpBody(values));
      if (turnOn !== null) await patchConfig("notifications.channels.email", { enabled: turnOn });
    },
    onSuccess: async () => {
      await refresh();
      setDraft({});
      setEnabled(null);
      test.reset();
      toast.success("Saved the email settings");
    },
    onSettled: () => {
      setEdited(new Set());
    },
  });
  const split = splitConfigErrors(save.error, SMTP_FIELDS, SMTP_CONFIG_KEYS);
  const errorOf = (name: SmtpField): string | undefined => (edited.has(name) ? undefined : split.fields[name]);

  const set = <K extends keyof SmtpForm>(name: K, value: SmtpForm[K], field: SmtpField = name): void => {
    setDraft((current) => ({ ...current, [name]: value }));
    setEdited((current) => new Set([...current, field]));
  };

  const configured = smtp.data !== undefined && smtp.data.host !== "" && smtp.data.recipients.length > 0;
  const testReason =
    smtp.data === undefined
      ? undefined
      : !configured
        ? "Set up the SMTP server and a recipient to test it."
        : dirty
          ? "Save your changes first."
          : !enabledStored
            ? "Turn on email notifications to test them."
            : undefined;

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!dirty || save.isPending || form === undefined) return;
    save.mutate({ values: smtpDirty ? form : null, turnOn: enabledDirty ? enabledValue : null });
  };

  const security = SMTP_SECURITY.find((option) => option.value === form?.security) ?? SMTP_SECURITY[0];

  return (
    <article aria-labelledby={headingId} className="flex min-w-0 flex-col gap-3 px-5 py-4">
      <ChannelHeader
        id={headingId}
        label="Email"
        description={DESCRIPTION}
        configured={configured}
        actions={<TestButton test={test} disabled={testReason !== undefined || smtp.data === undefined} reason={testReason} />}
      />
      {smtp.isError && smtp.data === undefined ? (
        <ErrorBlock compact error={smtp.error} title="Could not read the SMTP settings" onRetry={() => void smtp.refetch()} />
      ) : form === undefined ? (
        <EmailFormSkeleton />
      ) : (
        <form noValidate onSubmit={submit} className="flex min-w-0 flex-col gap-3">
          {split.form !== null ? <ErrorBlock live compact error={split.form} title="Could not save email" /> : null}
          <Checkbox
            label="Send notifications by email"
            checked={enabledValue}
            disabled={save.isPending}
            onCheckedChange={(next) => {
              setEnabled(next);
            }}
          />
          <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,1fr)_8rem]">
            <Field label="SMTP server" error={errorOf("host")}>
              <Input
                mono
                autoComplete="off"
                spellCheck={false}
                placeholder="smtp.example.com"
                value={form.host}
                disabled={save.isPending}
                onValueChange={(next: string) => {
                  set("host", next);
                }}
              />
            </Field>
            <Field label="Port" error={errorOf("port")}>
              <Input
                mono
                inputMode="numeric"
                autoComplete="off"
                value={form.port}
                disabled={save.isPending}
                onValueChange={(next: string) => {
                  set("port", next);
                }}
              />
            </Field>
          </div>
          <div className="flex min-w-0 flex-col gap-1.5">
            {/* The radio group is named by its own label; this is the same word, for sight. */}
            <span aria-hidden="true" className="text-13 font-medium text-fg">
              Encryption
            </span>
            <SegmentedControl<SmtpSecurity>
              label="Encryption"
              options={SMTP_SECURITY}
              value={form.security}
              onValueChange={(next) => {
                if (save.isPending) return;
                setDraft((current) => ({ ...current, security: next, port: portForSecurity(form.port, form.security, next) }));
                setEdited((current) => new Set([...current, "security", "port"]));
              }}
              className="self-start"
            />
            <p className="text-12 text-fg-muted">{security?.description}</p>
            {errorOf("security") !== undefined ? <p className="text-13 text-fail">{errorOf("security")}</p> : null}
          </div>
          <div className="grid min-w-0 gap-3 sm:grid-cols-2">
            <Field label="Username" optional error={errorOf("username")} description="Empty for a relay that takes mail without signing in.">
              <Input
                mono
                autoComplete="off"
                spellCheck={false}
                value={form.username}
                disabled={save.isPending}
                onValueChange={(next: string) => {
                  set("username", next);
                }}
              />
            </Field>
            <Field label="Password" optional error={errorOf("password")} description="Write-only: once saved it is never shown again.">
              <SecretInput
                label="Password"
                placeholder=""
                value={form.password}
                configured={smtp.data?.password_set === true}
                disabled={save.isPending}
                onChange={(next) => {
                  set("password", next);
                }}
              />
            </Field>
          </div>
          <Field label="From address" optional error={errorOf("from_address")} description="The sender recipients see. The username is used when it is empty.">
            <Input
              mono
              type="email"
              autoComplete="off"
              spellCheck={false}
              placeholder="wasm@example.com"
              value={form.from_address}
              disabled={save.isPending}
              onValueChange={(next: string) => {
                set("from_address", next);
              }}
            />
          </Field>
          <RecipientsEditor
            recipients={form.recipients}
            error={errorOf("recipients")}
            disabled={save.isPending}
            onChange={(next) => {
              set("recipients", next);
            }}
          />
          <DirtyActions
            dirty={dirty}
            pending={save.isPending}
            onDiscard={() => {
              setDraft({});
              setEnabled(null);
              setEdited(new Set());
              save.reset();
            }}
            note="A test goes to what is saved."
          />
        </form>
      )}
      <div role="status" className="min-w-0 empty:hidden">
        <TestOutcome result={test.data} error={test.error} source="the SMTP server" />
      </div>
    </article>
  );
}

/**
 * What every notification channel's card is built from: its header, a write-only secret field,
 * the test button and what the test answered, and the Save and Discard pair.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, Send } from "lucide-react";
import { useId, useState } from "react";
import type { ReactNode } from "react";

import { configKeys, testNotificationChannel } from "../../api/queries/config";
import type { NotificationTestResult } from "../../api/queries/config";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { StatusGlyph, StatusPill } from "../../components/ui/StatusPill";
import { SystemOutput } from "../../components/ui/SystemOutput";

/** Refreshes every configuration answer, the typed sections included. */
export function useRefreshConfig() {
  const queryClient = useQueryClient();
  return (): Promise<void> => queryClient.invalidateQueries({ queryKey: configKeys.all });
}

/**
 * What a test answered: the confirmation, or the failure in the receiving server's own words.
 *
 * @param source Who said it, as a sentence continues: "the server", "Telegram".
 */
export function TestOutcome({ result, error, source = "the server" }: { result: NotificationTestResult | undefined; error: unknown; source?: string }) {
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
  const speaker = source.charAt(0).toUpperCase() + source.slice(1);
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <p className="flex items-center gap-2 text-13 font-medium text-fg">
        <StatusGlyph state="failed" className="text-fail" />
        {`The test failed. ${speaker} said:`}
      </p>
      <SystemOutput label={`What ${source} said`} maxHeight="max-h-40" className="rounded-control border border-border bg-bg-sunken px-3 py-2">
        {result.detail}
      </SystemOutput>
    </div>
  );
}

/** A write-only field: what is typed can be shown, what is stored never comes back. */
export function SecretInput({
  label,
  placeholder,
  value,
  configured,
  onChange,
  disabled,
}: {
  /** The field's label, for the show and hide button. */
  label: string;
  placeholder: string;
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
      placeholder={configured ? "Set - leave blank to keep it" : placeholder}
      value={value}
      disabled={disabled}
      onValueChange={(next: string) => {
        onChange(next);
      }}
      suffix={
        <IconButton
          size="sm"
          label={shown ? `Hide the ${label.toLowerCase()}` : `Show the ${label.toLowerCase()}`}
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

export function useChannelTest(channel: string) {
  return useMutation({ mutationFn: () => testNotificationChannel(channel) });
}

/** Sending a test needs a destination to send to; a dirty form needs saving before it means anything. */
export function TestButton({ test, disabled, reason }: { test: ReturnType<typeof useChannelTest>; disabled: boolean; reason?: string | undefined }) {
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
export function ChannelHeader({
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
export function DirtyActions({ dirty, pending, onDiscard, note }: { dirty: boolean; pending: boolean; onDiscard: () => void; note?: string }) {
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

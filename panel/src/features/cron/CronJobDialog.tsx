import { useState } from "react";

import { isApiError } from "../../api/client";
import { RelativeTime } from "../../components/page/RelativeTime";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Skeleton } from "../../components/ui/Skeleton";
import { SystemOutput } from "../../components/ui/SystemOutput";
import type { CronJob, Schedule } from "./data";
import { SCHEDULE_PRESETS, absoluteWithOffset } from "./data";
import type { CreateCronJobBody } from "./useCronActions";
import { useCronActions } from "./useCronActions";
import { useCronPreview } from "./useCronPreview";

export interface CronJobDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Present to rewrite an existing job; absent to create one. */
  job?: CronJob;
}

/**
 * The refusal `POST /api/cron/preview` answered with, in the shape that fits: a schedule too
 * short, too long, or built from the wrong characters never reaches systemd - the request
 * model's own field validator refuses it first, as a 422 whose per-field message carries the
 * words (the response's `detail` is only ever the generic "Validation failed" for that shape);
 * an expression systemd itself rejects raises past the request model, and `detail`/`hint` carry
 * its own message and output instead.
 */
function schedulePreviewError(error: unknown): { message: string; output: string | null } | null {
  if (!isApiError(error)) return null;
  const fieldMessage = error.fields?.["schedule"];
  if (fieldMessage !== undefined) return { message: fieldMessage, output: null };
  return { message: error.detail, output: error.hint };
}

/** The calendar and next runs `POST /api/cron/preview` answered, or why it refused to. */
function SchedulePreview({ schedule }: { schedule: string }) {
  const preview = useCronPreview(schedule);

  if (schedule.trim() === "") return null;

  if (preview.isError) {
    const refusal = schedulePreviewError(preview.error) ?? { message: "The schedule could not be checked.", output: null };
    return (
      <div role="alert" className="flex flex-col gap-1.5">
        <p className="text-13 text-fail">{refusal.message}</p>
        {refusal.output !== null && refusal.output.trim() !== "" ? (
          <SystemOutput label="What systemd said" maxHeight="max-h-28">
            {refusal.output}
          </SystemOutput>
        ) : null}
      </div>
    );
  }

  if (preview.data === undefined) {
    return (
      <div aria-busy="true" className="flex flex-col gap-1.5">
        <span className="sr-only">Checking the schedule</span>
        <Skeleton className="h-3.5 w-48" />
        <Skeleton className="h-3.5 w-64" />
        <Skeleton className="h-3.5 w-56" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 rounded-control border border-border bg-bg-sunken p-3">
      <p className="mono text-12 text-fg-muted">{preview.data.calendar}</p>
      {preview.data.next_runs.length === 0 ? (
        <p className="text-13 text-fg-muted">This schedule has no future run.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {preview.data.next_runs.map((run) => (
            <li key={run} className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-13">
              <span className="mono text-12 text-fg-muted">{absoluteWithOffset(run)}</span>
              <RelativeTime value={run} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Creates a cron job, or rewrites one WASM already owns - `POST /api/cron` does both. The
 * schedule previews live through `POST /api/cron/preview` as it is typed or a preset is
 * chosen (`SchedulePreview`, debounced by `useCronPreview`): the normalised calendar and next
 * runs when it validates, systemd's own refusal when it does not.
 */
export function CronJobDialog({ open, onOpenChange, job }: CronJobDialogProps) {
  const editing = job !== undefined;
  const { create } = useCronActions();
  const [name, setName] = useState(job?.name ?? "");
  const [command, setCommand] = useState(job?.command ?? "");
  const [preset, setPreset] = useState<Schedule>((job?.schedule as Schedule | undefined) ?? "daily");
  const [calendar, setCalendar] = useState(job?.on_calendar ?? "");
  const [user, setUser] = useState(job?.user ?? "");
  const [workingDirectory, setWorkingDirectory] = useState(job?.working_directory ?? "");

  const close = (next: boolean): void => {
    if (!next && create.isPending) return;
    onOpenChange(next);
    if (!next) create.reset();
  };

  // The alias travels as-is: CronManager expands hourly/daily/weekly/monthly itself
  // (SCHEDULE_ALIASES), so the dialog does not keep its own copy of what each one means.
  const schedule = preset === "custom" ? calendar.trim() : preset;
  const valid = name.trim() !== "" && command.trim() !== "" && schedule !== "";

  const submit = (): void => {
    if (!valid) return;
    const body: CreateCronJobBody = {
      name: name.trim(),
      command: command.trim(),
      schedule,
      ...(user.trim() !== "" ? { user: user.trim() } : {}),
      ...(workingDirectory.trim() !== "" ? { working_directory: workingDirectory.trim() } : {}),
      // Saving an existing job rewrites it: without its application it would lose the link
      // (and the working directory default that follows the app's layout).
      ...(job?.app_domain ? { app_domain: job.app_domain } : {}),
    };
    create.mutate(body, { onSuccess: () => close(false) });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      size="md"
      title={editing ? `Edit ${job.name}` : "New cron job"}
      description="Runs a command on a schedule, as a systemd timer."
      footer={
        <>
          <Button disabled={create.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!valid} loading={create.isPending} onClick={submit}>
            {editing ? "Save job" : "Create job"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Name" name="name">
          <Input
            value={name}
            onValueChange={setName}
            mono
            disabled={editing}
            placeholder="nightly-report"
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="Command" name="command" description="Run as an argv, without a shell.">
          <Input
            value={command}
            onValueChange={setCommand}
            mono
            placeholder="/usr/bin/wasm backup create example.com"
            autoComplete="off"
            spellCheck={false}
          />
        </Field>
        <Field label="Schedule" name="schedule" nativeLabel={false}>
          <Select value={preset} onValueChange={setPreset} options={SCHEDULE_PRESETS} />
        </Field>
        {preset === "custom" ? (
          <Field label="Calendar expression" name="calendar" description="A systemd OnCalendar expression.">
            <Input
              value={calendar}
              onValueChange={setCalendar}
              mono
              placeholder="Mon..Fri *-*-* 09:00:00"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
        ) : null}
        <SchedulePreview schedule={schedule} />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="User" name="user" optional description="Defaults to the configured service user.">
            <Input value={user} onValueChange={setUser} mono autoComplete="off" spellCheck={false} />
          </Field>
          <Field label="Working directory" name="working_directory" optional>
            <Input value={workingDirectory} onValueChange={setWorkingDirectory} mono autoComplete="off" spellCheck={false} />
          </Field>
        </div>
        {create.isError ? (
          <ErrorBlock live compact error={create.error} title={editing ? "The job was not saved" : "The job was not created"} />
        ) : null}
      </div>
    </Dialog>
  );
}

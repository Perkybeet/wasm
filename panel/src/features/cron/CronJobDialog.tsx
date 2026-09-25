import { useState } from "react";

import { ErrorBlock } from "../../components/page/QueryState";
import { RelativeTime } from "../../components/page/RelativeTime";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import type { CronJob, Schedule } from "./data";
import { SCHEDULE_PRESETS } from "./data";
import type { CreateCronJobBody } from "./useCronActions";
import { useCronActions } from "./useCronActions";

const PRESET_CALENDAR: Record<Exclude<Schedule, "custom">, string> = {
  hourly: "*-*-* *:00:00",
  daily: "*-*-* 02:00:00",
  weekly: "Mon *-*-* 02:00:00",
  monthly: "*-*-01 02:00:00",
};

export interface CronJobDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Present to rewrite an existing job; absent to create one. */
  job?: CronJob;
}

/**
 * Creates a cron job, or rewrites one WASM already owns - `POST /api/cron` does both. There is
 * no endpoint to preview a calendar expression's upcoming runs before saving (see
 * `features/cron/data.ts`), so this shows what the backend validates on submit and, once
 * saved, the one next run the job reports.
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

  const schedule = preset === "custom" ? calendar.trim() : PRESET_CALENDAR[preset];
  const valid = name.trim() !== "" && command.trim() !== "" && schedule !== "";

  const submit = (): void => {
    if (!valid) return;
    const body: CreateCronJobBody = {
      name: name.trim(),
      command: command.trim(),
      schedule,
      ...(user.trim() !== "" ? { user: user.trim() } : {}),
      ...(workingDirectory.trim() !== "" ? { working_directory: workingDirectory.trim() } : {}),
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
          <div className="flex flex-col gap-2">
            <Select
              aria-label="Schedule preset"
              value={preset}
              onValueChange={setPreset}
              options={SCHEDULE_PRESETS}
            />
            {preset === "custom" ? (
              <Input
                aria-label="Calendar expression"
                value={calendar}
                onValueChange={setCalendar}
                mono
                placeholder="Mon..Fri *-*-* 09:00:00"
                autoComplete="off"
                spellCheck={false}
              />
            ) : (
              <p className="mono text-12 text-fg-faint">{PRESET_CALENDAR[preset]}</p>
            )}
          </div>
        </Field>
        <p className="text-12 text-fg-muted">
          WASM does not preview upcoming runs before saving - the systemd calendar engine is the only implementation
          of what a schedule means, and the panel defers to it. The next run appears below once the job is saved.
        </p>
        {editing && job.enabled ? (
          <p className="text-13 text-fg">
            Next run: <RelativeTime value={job.next_run} fallback={job.next_run} />
          </p>
        ) : null}
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

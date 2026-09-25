import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { appsQuery } from "../../api/queries/apps";
import type { BackupSchedule } from "../../api/queries/backups";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { useBackupActions } from "./useBackupActions";

const PRESETS = [
  { value: "hourly", label: "Hourly" },
  { value: "daily", label: "Daily, 02:00" },
  { value: "weekly", label: "Weekly, Monday 02:00" },
  { value: "monthly", label: "Monthly, 1st at 02:00" },
  { value: "custom", label: "Custom (systemd OnCalendar)" },
];

const KNOWN = new Set(["hourly", "daily", "weekly", "monthly"]);

export interface ScheduleDialogProps {
  /** Present to edit a schedule; absent to create one. */
  existing?: BackupSchedule;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/** Creates or edits an application's backup schedule: a preset or a raw systemd calendar expression. */
export function ScheduleDialog({ existing, open, onOpenChange }: ScheduleDialogProps) {
  const formId = useId();
  const apps = useQuery({ ...appsQuery(), enabled: open && existing === undefined });
  const [domain, setDomain] = useState(existing?.domain ?? "");
  const [preset, setPreset] = useState(existing !== undefined && KNOWN.has(existing.schedule) ? existing.schedule : "daily");
  const [customCalendar, setCustomCalendar] = useState(existing?.on_calendar ?? "");
  const [retentionCount, setRetentionCount] = useState(String(existing?.retention_count ?? 7));
  const [retentionDays, setRetentionDays] = useState(String(existing?.retention_days ?? 30));
  const [includeDatabases, setIncludeDatabases] = useState(true);
  const { createSchedule } = useBackupActions();

  const close = (next: boolean): void => {
    if (!next && createSchedule.isPending) return;
    onOpenChange(next);
    if (!next) createSchedule.reset();
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    const targetDomain = existing?.domain ?? domain.trim();
    if (targetDomain === "") return;
    const schedule = preset === "custom" ? customCalendar.trim() : preset;
    if (schedule === "") return;
    createSchedule.mutate(
      {
        domain: targetDomain,
        schedule,
        retentionCount: Number(retentionCount) || 7,
        retentionDays: Number(retentionDays) || 30,
        includeDatabases,
      },
      { onSuccess: () => close(false) },
    );
  };

  const domainOptions = (apps.data?.apps ?? []).map((app) => ({ value: app.domain, label: app.domain }));

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={existing ? `Edit the schedule for ${existing.domain}` : "New backup schedule"}
      description="Backs this application up on a systemd timer, with its own retention."
      footer={
        <>
          <Button disabled={createSchedule.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            variant="primary"
            loading={createSchedule.isPending}
            disabled={(existing === undefined && domain.trim() === "") || (preset === "custom" && customCalendar.trim() === "")}
          >
            {existing ? "Save" : "Create schedule"}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="flex flex-col gap-4">
        {existing === undefined ? (
          <Field label="Application" nativeLabel={false}>
            <Select
              aria-label="Application"
              value={domain}
              onValueChange={setDomain}
              placeholder={apps.isPending ? "Loading applications..." : "Choose an application"}
              options={domainOptions}
              disabled={apps.isPending || domainOptions.length === 0}
            />
          </Field>
        ) : null}
        <Field label="Schedule" name="schedule" nativeLabel={false}>
          <Select aria-label="Schedule" value={preset} onValueChange={setPreset} options={PRESETS} />
        </Field>
        {preset === "custom" ? (
          <Field label="OnCalendar expression" description="Systemd calendar syntax, e.g. *-*-* 03:30:00.">
            <Input mono value={customCalendar} onValueChange={setCustomCalendar} placeholder="*-*-* 03:30:00" autoComplete="off" spellCheck={false} />
          </Field>
        ) : null}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Keep" description="Backups to keep.">
            <Input mono inputMode="numeric" value={retentionCount} onValueChange={setRetentionCount} />
          </Field>
          <Field label="Max age" description="Days before a backup is pruned.">
            <Input mono inputMode="numeric" value={retentionDays} onValueChange={setRetentionDays} />
          </Field>
        </div>
        <Checkbox checked={includeDatabases} onCheckedChange={setIncludeDatabases} label="Dump databases too" />
        {createSchedule.isError ? <ErrorBlock live compact error={createSchedule.error} title="The schedule was not saved" /> : null}
      </form>
    </Dialog>
  );
}

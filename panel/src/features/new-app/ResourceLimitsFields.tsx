import { ChevronDown } from "lucide-react";

import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import type { LimitsDraft } from "../app/settings/limits";
import { limitField } from "./wizard";
import type { ReviewErrors } from "./wizard";

export interface ResourceLimitsFieldsProps {
  draft: LimitsDraft;
  /** CPUs of this machine, when known, for the CPU quota's description. */
  cores: number | null;
  errors: ReviewErrors;
  onChange: (draft: LimitsDraft) => void;
}

/**
 * Memory, CPU and task limits for the unit systemd will run, collapsed by default: most
 * deploys need none, and the ones that do are the exception, not the rule. An empty field is
 * no limit, exactly as `PATCH /api/apps/{domain}/limits` on the app's own Settings tab treats
 * one - the same bounds and the same words, so the two never disagree.
 */
export function ResourceLimitsFields({ draft, cores, errors, onChange }: ResourceLimitsFieldsProps) {
  const set = (patch: Partial<LimitsDraft>): void => {
    onChange({ ...draft, ...patch });
  };
  const invalid = errors[limitField("memory")] !== undefined || errors[limitField("cpu")] !== undefined || errors[limitField("tasks")] !== undefined;

  return (
    <details open={invalid} className="group rounded-control border border-border">
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2.5 text-13 font-medium text-fg [&::-webkit-details-marker]:hidden">
        <span>Resource limits</span>
        <span className="flex items-center gap-1.5 text-12 font-normal text-fg-muted">
          <span className="group-open:hidden">Empty: no limit</span>
          <span className="hidden group-open:inline">Hide</span>
          <ChevronDown aria-hidden="true" className="size-3.5 transition-transform duration-(--duration-fast) group-open:rotate-180" />
        </span>
      </summary>
      <div className="grid gap-4 border-t border-border px-3 py-3 sm:grid-cols-3">
        <Field label="Memory" description="MemoryMax. At least 64 MB." error={errors[limitField("memory")]}>
          <Input
            mono
            inputMode="numeric"
            autoComplete="off"
            placeholder="No limit"
            suffix="MB"
            value={draft.memory}
            onValueChange={(value: string) => set({ memory: value })}
          />
        </Field>
        <Field
          label="CPU"
          description={cores === null ? "CPUQuota. 100 is one whole CPU, 200 two." : `CPUQuota. 100 is one whole CPU; up to ${String(100 * cores)} here.`}
          error={errors[limitField("cpu")]}
        >
          <Input
            mono
            inputMode="numeric"
            autoComplete="off"
            placeholder="No limit"
            suffix="%"
            value={draft.cpu}
            onValueChange={(value: string) => set({ cpu: value })}
          />
        </Field>
        <Field label="Tasks" description="TasksMax: processes and threads. At least 16." error={errors[limitField("tasks")]}>
          <Input mono inputMode="numeric" autoComplete="off" placeholder="No limit" value={draft.tasks} onValueChange={(value: string) => set({ tasks: value })} />
        </Field>
      </div>
    </details>
  );
}

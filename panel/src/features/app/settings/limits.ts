/**
 * The resource limits form: what each field accepts and how its value reaches
 * `PATCH /api/apps/{domain}/limits`.
 *
 * The bounds and the words mirror `wasm.managers.service_manager.ResourceLimits.validate`,
 * which is the check that counts; this one only saves a round trip. An empty field removes the
 * limit, exactly as a null does in the request.
 */

/** MemoryMax below this does not leave a runtime room to start (MIN_MEMORY_MB). */
export const MIN_MEMORY_MB = 64;

/** TasksMax below this starves a Node or Python runtime of threads (MIN_TASKS). */
export const MIN_TASKS = 16;

export interface LimitsDraft {
  memory: string;
  cpu: string;
  tasks: string;
}

export interface LimitsValues {
  memory_max_mb: number | null;
  cpu_quota_percent: number | null;
  tasks_max: number | null;
}

export type LimitsErrors = Partial<Record<keyof LimitsDraft, string>>;

export interface ParsedLimits {
  values: LimitsValues;
  errors: LimitsErrors;
}

const WHOLE = /^\d+$/;

/** A field's value: null when empty (no limit), a number when whole, undefined when neither. */
function whole(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  if (!WHOLE.test(trimmed)) return undefined;
  return Number.parseInt(trimmed, 10);
}

const NOT_A_NUMBER = "Enter a whole number, or leave it empty for no limit.";

/**
 * Reads the form, with the backend's own wording for a value out of range.
 *
 * @param cores CPUs of the machine, when known; CPUQuota runs to 100% per CPU.
 */
export function parseLimits(draft: LimitsDraft, cores: number | null): ParsedLimits {
  const errors: LimitsErrors = {};

  const memory = whole(draft.memory);
  if (memory === undefined) errors.memory = NOT_A_NUMBER;
  else if (memory !== null && memory < MIN_MEMORY_MB)
    errors.memory = `A memory limit of ${String(memory)}M is too small. Allow at least ${String(MIN_MEMORY_MB)}M, or no limit.`;

  const cpu = whole(draft.cpu);
  if (cpu === undefined) errors.cpu = NOT_A_NUMBER;
  else if (cpu !== null && (cpu < 1 || (cores !== null && cpu > 100 * cores))) {
    errors.cpu =
      cores === null
        ? `A CPU quota of ${String(cpu)}% is not possible. Use at least 1%.`
        : `A CPU quota of ${String(cpu)}% is not possible here. This machine has ${String(cores)} CPU${cores === 1 ? "" : "s"}: use 1% to ${String(100 * cores)}%.`;
  }

  const tasks = whole(draft.tasks);
  if (tasks === undefined) errors.tasks = NOT_A_NUMBER;
  else if (tasks !== null && tasks < MIN_TASKS)
    errors.tasks = `A limit of ${String(tasks)} tasks is too small. Allow at least ${String(MIN_TASKS)}, or no limit.`;

  return {
    values: {
      memory_max_mb: memory ?? null,
      cpu_quota_percent: cpu ?? null,
      tasks_max: tasks ?? null,
    },
    errors,
  };
}

/** The form's starting text for limits an app has now. */
export function draftOf(values: Partial<LimitsValues>): LimitsDraft {
  const text = (value: number | null | undefined): string => (typeof value === "number" && value > 0 ? String(value) : "");
  return { memory: text(values.memory_max_mb), cpu: text(values.cpu_quota_percent), tasks: text(values.tasks_max) };
}

/** Whether the form says something different from what the app has. */
export function changed(draft: LimitsDraft, current: LimitsDraft): boolean {
  return draft.memory.trim() !== current.memory || draft.cpu.trim() !== current.cpu || draft.tasks.trim() !== current.tasks;
}

/** The limits as systemd directives, the way `wasm app limits` prints them. */
export function directives(values: Partial<LimitsValues>): string[] {
  const lines: string[] = [];
  if (values.memory_max_mb) lines.push(`MemoryMax=${String(values.memory_max_mb)}M`);
  if (values.cpu_quota_percent) lines.push(`CPUQuota=${String(values.cpu_quota_percent)}%`);
  if (values.tasks_max) lines.push(`TasksMax=${String(values.tasks_max)}`);
  return lines;
}

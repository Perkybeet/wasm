import { Check } from "lucide-react";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import { STEPS } from "./wizard";
import type { Step } from "./wizard";

export interface StepRailProps {
  current: Step;
  /** What each finished step settled, shown under its name: the source, the domain. */
  notes: Partial<Record<Step, ReactNode>>;
  /** Steps that can be gone back to. Later steps never can: they need the earlier ones. */
  onGoTo: (step: Step) => void;
  /** Nothing can be revisited while a request is in flight. */
  locked?: boolean;
}

/**
 * Where the operator is in the three steps: a numbered list, because it is a sequence. A
 * finished step is a button back to it; the current one is marked for assistive technology.
 */
export function StepRail({ current, notes, onGoTo, locked = false }: StepRailProps) {
  const at = STEPS.findIndex((step) => step.id === current);
  return (
    <nav aria-label="Steps">
      <ol className="flex gap-2 lg:flex-col lg:gap-1">
        {STEPS.map((step, index) => {
          const done = index < at;
          const here = index === at;
          const marker = (
            <span
              aria-hidden="true"
              className={cx(
                "mono flex size-6 shrink-0 items-center justify-center rounded-pill border text-12",
                here && "border-accent bg-accent text-on-accent",
                done && "border-border-strong bg-surface text-fg",
                !here && !done && "border-border bg-bg-sunken text-fg-faint",
              )}
            >
              {done ? <Check className="size-3.5" strokeWidth={2.5} /> : index + 1}
            </span>
          );
          const text = (
            <span className="flex min-w-0 flex-col">
              <span className={cx("text-13 font-medium", here || done ? "text-fg" : "text-fg-muted")}>
                {step.label}
                <span className="sr-only">{done ? ", done" : here ? ", current step" : ""}</span>
              </span>
              {notes[step.id] !== undefined && done ? (
                <span className="hidden min-w-0 truncate text-12 text-fg-muted lg:block">{notes[step.id]}</span>
              ) : null}
            </span>
          );
          return (
            <li key={step.id} className="min-w-0 flex-1 lg:flex-none" {...(here ? { "aria-current": "step" } : {})}>
              {done && !locked ? (
                <button
                  type="button"
                  onClick={() => onGoTo(step.id)}
                  className="flex w-full min-w-0 cursor-pointer items-center gap-2.5 rounded-control px-2 py-1.5 text-left hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus"
                >
                  {marker}
                  {text}
                </button>
              ) : (
                <div className="flex min-w-0 items-center gap-2.5 px-2 py-1.5">
                  {marker}
                  {text}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

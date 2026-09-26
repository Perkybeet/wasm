import { CircleAlert, TriangleAlert } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { SegmentedControl } from "../../../components/page/SegmentedControl";
import { Button } from "../../../components/ui/Button";
import { Dialog } from "../../../components/ui/Dialog";
import { Field } from "../../../components/ui/Field";
import { Textarea } from "../../../components/ui/Textarea";
import { cx } from "../../../lib/cx";
import { formatCount } from "../../../lib/format";
import type { DraftOp, EnvMap } from "./draft";
import { nameProblem, parseDotenv, valueProblem } from "./dotenv";

export type PasteMode = "merge" | "replace";

const MODES = [
  { value: "merge", label: "Add and update" },
  { value: "replace", label: "Replace all" },
] as const;

interface Problem {
  /** Blocks staging: the API would refuse the variables. */
  blocking: boolean;
  text: string;
}

/** What is wrong with the pasted text, line by line, in the order the lines come. */
function problemsOf(text: string): { problems: Problem[]; count: number; parsed: ReturnType<typeof parseDotenv> } {
  const parsed = parseDotenv(text);
  const problems: Problem[] = [];
  const lines = new Map<string, number[]>();
  for (const assignment of parsed.assignments) {
    lines.set(assignment.name, [...(lines.get(assignment.name) ?? []), assignment.line]);
    const name = nameProblem(assignment.name);
    if (name !== null) problems.push({ blocking: true, text: `Line ${String(assignment.line)}: ${name}` });
    const value = valueProblem(assignment.value);
    if (value !== null) problems.push({ blocking: true, text: `Line ${String(assignment.line)}: ${value}` });
  }
  for (const [name, at] of lines) {
    if (at.length > 1 && nameProblem(name) === null) {
      const list = at.map(String);
      problems.push({
        blocking: false,
        text: `${name} is set on lines ${list.slice(0, -1).join(", ")} and ${list.at(-1) ?? ""}; the later line wins.`,
      });
    }
  }
  for (const skipped of parsed.skipped) {
    problems.push({ blocking: false, text: `Line ${String(skipped.line)} has no = and is skipped, as WASM skips it.` });
  }
  return { problems, count: parsed.variables.size, parsed };
}

export interface PasteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The variables the file holds now (masked), to say what each pasted one does. */
  current: EnvMap;
  onStage: (ops: DraftOp[], summary: string) => void;
}

/**
 * A whole `.env` pasted at once, parsed live the way WASM reads the file on disk, with what
 * is wrong said line by line before anything is staged.
 */
export function PasteDialog({ open, onOpenChange, current, onStage }: PasteDialogProps) {
  const formId = useId();
  const [text, setText] = useState("");
  const [mode, setMode] = useState<PasteMode>("merge");
  const { problems, count, parsed } = useMemo(() => problemsOf(text), [text]);
  const blocking = problems.some((problem) => problem.blocking);

  const removals = mode === "replace" ? [...current.keys()].filter((name) => !parsed.variables.has(name)).length : 0;

  const close = (next: boolean): void => {
    onOpenChange(next);
    if (!next) {
      setText("");
      setMode("merge");
    }
  };

  const stage = (): void => {
    if (count === 0 || blocking) return;
    const ops: DraftOp[] =
      mode === "replace"
        ? [{ kind: "replace", variables: new Map(parsed.variables) }]
        : [...parsed.variables].map(([name, value]) => ({ kind: "set", name, value }));
    const summary =
      mode === "replace"
        ? `Staged ${formatCount(count)} pasted ${count === 1 ? "variable" : "variables"} to replace the file`
        : `Staged ${formatCount(count)} pasted ${count === 1 ? "variable" : "variables"}`;
    onStage(ops, summary);
    close(false);
  };

  const noun = count === 1 ? "variable" : "variables";

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      size="lg"
      title="Paste a .env file"
      description="Read the way WASM reads the file on disk: blank lines, comments and one pair of quotes around a value are dropped, nothing else is interpreted. Nothing is saved until you review the changes."
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button type="submit" form={formId} variant="primary" disabled={count === 0 || blocking}>
            {count === 0 ? "Stage variables" : `Stage ${formatCount(count)} ${noun}`}
          </Button>
        </>
      }
    >
      <form
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          stage();
        }}
        className="flex flex-col gap-4"
      >
        {/* Side by side from the small breakpoint: the text and what it parses to. */}
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label=".env contents">
            <Textarea
              mono
              rows={11}
              wrap="off"
              autoComplete="off"
              spellCheck={false}
              placeholder={"NODE_ENV=production\nDATABASE_URL=postgres://app:secret@127.0.0.1:5432/app"}
              value={text}
              onChange={(event) => {
                setText(event.target.value);
              }}
              className="h-60 [&_textarea]:h-full [&_textarea]:resize-none"
            />
          </Field>

          <section aria-labelledby={`${formId}-preview`} className="flex min-w-0 flex-col gap-1.5">
            <h3 id={`${formId}-preview`} className="text-13 font-medium text-fg">
              {count === 0 ? "Parsed variables" : `${formatCount(count)} ${noun} found`}
            </h3>
            <div
              role="region"
              aria-label="What the pasted text parses to"
              tabIndex={0}
              className="flex h-60 flex-col gap-2 overflow-y-auto rounded-control border border-border bg-bg-sunken p-2 scroll-thin focus-visible:outline-2 focus-visible:outline-focus"
            >
              {count === 0 && problems.length === 0 ? (
                <p className="px-1 py-1 text-12 text-fg-faint">Each NAME=value line appears here as it will be read.</p>
              ) : null}
              {problems.length > 0 ? (
                <ul className="flex flex-col gap-1.5 px-1 pt-0.5">
                  {problems.map((problem, index) => (
                    <li key={index} className="flex items-start gap-1.5 text-12">
                      {problem.blocking ? (
                        <CircleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-fail" />
                      ) : (
                        <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-warn" />
                      )}
                      <span className={problem.blocking ? "text-fg" : "text-fg-muted"}>
                        <span className="sr-only">{problem.blocking ? "Error: " : "Note: "}</span>
                        {problem.text}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
              {count > 0 ? (
                <ul aria-label="Parsed variables" className="divide-y divide-border rounded-[4px] border border-border bg-surface">
                  {[...parsed.variables].map(([name, value]) => {
                    const invalid = nameProblem(name) !== null;
                    return (
                      <li key={name} className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-2 px-2 py-1 text-12">
                        <span translate="no" className={cx("mono truncate", invalid ? "text-fail" : "text-fg")} title={name}>
                          {name === "" ? "(no name)" : name}
                        </span>
                        <span className="text-fg-faint">{current.has(name) ? "Replaces" : "New"}</span>
                        <span translate="no" className="mono col-span-2 truncate text-fg-muted" title={value}>
                          {value === "" ? <span className="font-sans text-fg-faint">Empty</span> : value}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </div>
          </section>
        </div>


        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
            <span aria-hidden="true" className="text-13 font-medium text-fg">
              What to do with the pasted variables
            </span>
            <SegmentedControl<PasteMode>
              label="What to do with the pasted variables"
              options={MODES}
              value={mode}
              onValueChange={setMode}
            />
          </div>
          <p className="text-13 text-fg-muted">
            {mode === "merge"
              ? "They are added, or replace the value of a variable with the same name. The others stay."
              : removals > 0
                ? `The file will hold exactly these: ${formatCount(removals)} current ${removals === 1 ? "variable is" : "variables are"} removed.`
                : "The file will hold exactly these."}
          </p>
        </div>
      </form>
    </Dialog>
  );
}

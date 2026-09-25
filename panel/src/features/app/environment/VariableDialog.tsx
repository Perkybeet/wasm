import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import { Button } from "../../../components/ui/Button";
import { Dialog } from "../../../components/ui/Dialog";
import { Field } from "../../../components/ui/Field";
import { Input } from "../../../components/ui/Input";
import { nameProblem, readBack, valueProblem } from "./dotenv";

export type VariableTarget = { mode: "add" } | { mode: "edit"; name: string; value: string };

export interface VariableDialogProps {
  target: VariableTarget | null;
  /** Names the file has or the draft adds, for the duplicate check when adding. */
  existing: ReadonlySet<string>;
  onClose: () => void;
  onSubmit: (name: string, value: string) => void;
}

interface FormProps {
  formId: string;
  target: VariableTarget;
  existing: ReadonlySet<string>;
  onSubmit: (name: string, value: string) => void;
}

/** Mounted fresh for every opening, so it starts from the target's values. */
function VariableForm({ formId, target, existing, onSubmit }: FormProps) {
  const editing = target.mode === "edit";
  const [name, setName] = useState(editing ? target.name : "");
  const [value, setValue] = useState(editing ? target.value : "");
  const [submitted, setSubmitted] = useState(false);

  const trimmedName = name.trim();
  const duplicate = !editing && existing.has(trimmedName) ? `${trimmedName} is already set. Edit its row instead.` : null;
  const nameError = trimmedName === "" && !submitted ? null : (nameProblem(trimmedName) ?? duplicate);
  const valueError = valueProblem(value);
  const back = readBack(value);

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setSubmitted(true);
    if (nameProblem(trimmedName) !== null || duplicate !== null || valueError !== null) return;
    onSubmit(trimmedName, value);
  };

  return (
    <form id={formId} onSubmit={submit} noValidate className="flex flex-col gap-4">
      <Field
        label="Name"
        error={nameError}
        {...(editing ? {} : { description: "Letters, digits and underscores, starting with a letter or underscore." })}
      >
        <Input
          mono
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          placeholder="DATABASE_URL"
          value={name}
          disabled={editing}
          onValueChange={(next: string) => {
            setName(next);
          }}
        />
      </Field>
      <Field
        label="Value"
        error={valueError}
        description={
          valueError === null && back !== value
            ? `WASM writes values without quotes, so this one is read back as "${back}".`
            : "Stored as typed. Leave it empty for an empty value."
        }
      >
        <Input
          mono
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          value={value}
          onValueChange={(next: string) => {
            setValue(next);
          }}
        />
      </Field>
    </form>
  );
}

/**
 * Adds a variable or changes one, as a change saved later with the others. Checked against
 * what the API accepts before it is staged, so a draft can always be saved.
 */
export function VariableDialog({ target, existing, onClose, onSubmit }: VariableDialogProps) {
  const formId = useId();
  // The last target stays on screen while the dialog animates closed; each opening mounts a
  // fresh form, so an abandoned entry is not there the next time.
  const [shown, setShown] = useState<{ target: VariableTarget; generation: number } | null>(null);
  if (target !== null && target !== shown?.target) setShown({ target, generation: (shown?.generation ?? 0) + 1 });
  const current = target ?? shown?.target ?? null;
  const editing = current?.mode === "edit";
  return (
    <Dialog
      open={target !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      size="sm"
      title={current?.mode === "edit" ? `Edit ${current.name}` : "Add a variable"}
      description="The change waits with any others until you review and save them."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" form={formId} variant="primary">
            {editing ? "Update variable" : "Add variable"}
          </Button>
        </>
      }
    >
      {current !== null && shown !== null ? (
        <VariableForm key={shown.generation} formId={formId} target={current} existing={existing} onSubmit={onSubmit} />
      ) : null}
    </Dialog>
  );
}

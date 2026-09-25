import { Eye, EyeOff, KeyRound, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { generateSecret } from "./secrets";
import { envField, envNameField } from "./wizard";
import type { EnvRow, ReviewErrors } from "./wizard";

function Label({ row }: { row: EnvRow }) {
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
      <code translate="no" className="text-12 font-medium text-fg">
        {row.name}
      </code>
      {row.required ? <span className="text-12 font-medium text-fg-muted">Required</span> : null}
      {row.secret ? <span className="text-12 font-normal text-fg-faint">Secret</span> : null}
    </span>
  );
}

function DeclaredRow({ row, error, onChange }: { row: EnvRow; error: string | undefined; onChange: (value: string) => void }) {
  const [shown, setShown] = useState(false);
  const description =
    row.example !== null && row.example !== ""
      ? row.secret
        ? "Default from .env.example. Replace it: an example secret is public."
        : "Default from .env.example."
      : row.secret
        ? "Paste the value, or generate a random one."
        : undefined;
  return (
    <div className={row.secret ? "grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2" : undefined}>
      <Field label={<Label row={row} />} error={error} {...(description !== undefined ? { description } : {})}>
        <Input
          mono
          type={row.secret && !shown ? "password" : "text"}
          value={row.value}
          onValueChange={(value: string) => onChange(value)}
          autoComplete={row.secret ? "new-password" : "off"}
          autoCapitalize="off"
          spellCheck={false}
          {...(row.secret
            ? {
                suffix: (
                  <IconButton
                    label={shown ? `Hide ${row.name}` : `Show ${row.name}`}
                    icon={shown ? <EyeOff /> : <Eye />}
                    size="sm"
                    pressed={shown}
                    onClick={() => setShown(!shown)}
                  />
                ),
              }
            : {})}
        />
      </Field>
      {row.secret ? (
        <Button
          size="md"
          icon={<KeyRound aria-hidden="true" />}
          aria-label={`Generate ${row.name}`}
          onClick={() => onChange(generateSecret())}
          className="mt-[1.625rem]"
        >
          <span className="hidden sm:inline">Generate</span>
        </Button>
      ) : null}
    </div>
  );
}

function AddedRow({
  row,
  nameError,
  valueError,
  onChange,
  onRemove,
}: {
  row: EnvRow;
  nameError: string | undefined;
  valueError: string | undefined;
  onChange: (patch: Partial<EnvRow>) => void;
  onRemove: () => void;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)_auto] sm:items-start">
      <Field label="Name" error={nameError}>
        <Input
          mono
          value={row.name}
          onValueChange={(value: string) => onChange({ name: value })}
          placeholder="API_URL"
          autoComplete="off"
          autoCapitalize="characters"
          spellCheck={false}
        />
      </Field>
      <Field label="Value" error={valueError}>
        <Input mono value={row.value} onValueChange={(value: string) => onChange({ value })} autoComplete="off" spellCheck={false} />
      </Field>
      <IconButton
        label={row.name.trim() === "" ? "Remove this variable" : `Remove ${row.name.trim()}`}
        icon={<Trash2 />}
        onClick={onRemove}
        className="sm:mt-[1.625rem]"
      />
    </div>
  );
}

export interface EnvironmentFieldsProps {
  rows: EnvRow[];
  errors: ReviewErrors;
  onChange: (rows: EnvRow[]) => void;
}

let added = 0;

/**
 * The app's environment, generated from `.env.example`: one field per declared variable, its
 * default filled in, credentials masked with a generator beside them, the ones without a default
 * marked. More variables can be added; everything can be changed later from the app's
 * Environment tab.
 */
export function EnvironmentFields({ rows, errors, onChange }: EnvironmentFieldsProps) {
  const declared = rows.filter((row) => row.declared);
  const extra = rows.filter((row) => !row.declared);
  const update = (id: string, patch: Partial<EnvRow>): void => {
    onChange(rows.map((row) => (row.id === id ? { ...row, ...patch } : row)));
  };
  const add = (): void => {
    added += 1;
    onChange([
      ...rows,
      { id: `added:${String(added)}`, name: "", value: "", secret: false, required: false, declared: false, example: null },
    ]);
  };

  return (
    <div className="flex flex-col gap-4">
      {declared.length === 0 ? (
        <p className="text-13 text-fg-muted">
          The repository has no .env.example. Add the variables the app needs now, or later from its Environment tab.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          {declared.map((row) => (
            <DeclaredRow key={row.id} row={row} error={errors[envField(row)]} onChange={(value) => update(row.id, { value })} />
          ))}
        </div>
      )}
      {extra.length > 0 ? (
        <div className="flex flex-col gap-3 border-t border-border pt-4">
          {extra.map((row) => (
            <AddedRow
              key={row.id}
              row={row}
              nameError={errors[envNameField(row)]}
              valueError={errors[envField(row)]}
              onChange={(patch) => update(row.id, patch)}
              onRemove={() => onChange(rows.filter((other) => other.id !== row.id))}
            />
          ))}
        </div>
      ) : null}
      <div>
        <Button size="sm" icon={<Plus aria-hidden="true" />} onClick={add}>
          Add variable
        </Button>
      </div>
    </div>
  );
}

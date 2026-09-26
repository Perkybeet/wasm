import { Plus, Trash2 } from "lucide-react";

import { Button } from "../../components/ui/Button";
import { Field } from "../../components/ui/Field";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { pathField } from "./wizard";
import type { PathRow, ReviewErrors } from "./wizard";

export interface PersistentPathsFieldProps {
  rows: PathRow[];
  errors: ReviewErrors;
  onChange: (rows: PathRow[]) => void;
}

let added = 0;

/**
 * The paths that survive every release: linked into `shared/` and kept across every deploy,
 * for uploads or anything else a build must not throw away. Empty is the common case - most
 * apps keep no state on disk - so the list starts with nothing and the operator adds what
 * their app needs.
 */
export function PersistentPathsField({ rows, errors, onChange }: PersistentPathsFieldProps) {
  const update = (id: string, value: string): void => {
    onChange(rows.map((row) => (row.id === id ? { ...row, value } : row)));
  };
  const add = (): void => {
    added += 1;
    onChange([...rows, { id: `path:${String(added)}`, value: "" }]);
  };

  return (
    <div className="flex flex-col gap-3">
      {/* The label above says what these are; with none, the Add button is the whole story. */}
      {rows.length === 0 ? null : (
        rows.map((row) => (
          <div key={row.id} className="flex items-start gap-2">
            <Field label="Path" error={errors[pathField(row)]} className="min-w-0 flex-1">
              <Input
                mono
                value={row.value}
                onValueChange={(value: string) => update(row.id, value)}
                placeholder="storage"
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
              />
            </Field>
            <IconButton
              label={row.value.trim() === "" ? "Remove this path" : `Remove ${row.value.trim()}`}
              icon={<Trash2 />}
              onClick={() => onChange(rows.filter((other) => other.id !== row.id))}
              className="mt-[1.625rem]"
            />
          </div>
        ))
      )}
      <div>
        <Button size="sm" icon={<Plus aria-hidden="true" />} onClick={add}>
          Add path
        </Button>
      </div>
    </div>
  );
}

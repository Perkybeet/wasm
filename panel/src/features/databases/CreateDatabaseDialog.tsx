import { useId, useRef, useState } from "react";
import type { ReactElement, SyntheticEvent } from "react";

import type { Engine } from "../../api/queries/databases";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { engineLabel } from "./data";
import { useDatabaseActions } from "./useDatabaseActions";

export interface CreateDatabaseDialogProps {
  /** Engines installed and running, the only ones that can hold a new database. */
  engines: readonly Engine[];
  trigger: ReactElement<Record<string, unknown>>;
}

const NAME_HINT: Readonly<Record<string, string>> = {
  redis: "Redis databases are numbered slots (0-15 by default); name it with a number.",
};

/** Creates a database on one running engine. */
export function CreateDatabaseDialog({ engines, trigger }: CreateDatabaseDialogProps) {
  const formId = useId();
  const nameRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  // Not derived once at mount: `engines` starts empty while its query is loading, and a plain
  // useState would keep that empty default forever. Falling back live keeps the first running
  // engine selected once the list arrives, unless the operator already chose one themselves.
  const [chosenEngine, setEngine] = useState("");
  const engine = engines.some((item) => item.name === chosenEngine) ? chosenEngine : (engines[0]?.name ?? "");
  const [name, setName] = useState("");
  const [owner, setOwner] = useState("");
  const [encoding, setEncoding] = useState("");
  const { createDatabase } = useDatabaseActions();

  const close = (next: boolean): void => {
    if (!next && createDatabase.isPending) return;
    setOpen(next);
    if (!next) {
      setName("");
      setOwner("");
      setEncoding("");
      createDatabase.reset();
    }
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (name.trim() === "" || engine === "") return;
    createDatabase.mutate(
      { engine, name: name.trim(), owner: owner.trim() || undefined, encoding: encoding.trim() || undefined },
      { onSuccess: () => close(false) },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      trigger={trigger}
      initialFocus={nameRef}
      title="Create database"
      description="Creates an empty database on the chosen engine."
      footer={
        <>
          <Button disabled={createDatabase.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={createDatabase.isPending} disabled={name.trim() === ""}>
            Create database
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="flex flex-col gap-4">
        <Field label="Engine" nativeLabel={false}>
          <Select
            aria-label="Engine"
            value={engine}
            onValueChange={setEngine}
            options={engines.map((item) => ({ value: item.name, label: engineLabel(item.name) }))}
          />
        </Field>
        <Field label="Name" description={NAME_HINT[engine]}>
          <Input ref={nameRef} mono value={name} onValueChange={setName} autoComplete="off" spellCheck={false} />
        </Field>
        {engine !== "redis" ? (
          <>
            <Field label="Owner" optional description="A user that already exists. Defaults to the engine's superuser.">
              <Input mono value={owner} onValueChange={setOwner} autoComplete="off" spellCheck={false} />
            </Field>
            <Field label="Encoding" optional description="Defaults to UTF8 (PostgreSQL) or utf8mb4 (MySQL/MariaDB).">
              <Input mono value={encoding} onValueChange={setEncoding} placeholder="UTF8" autoComplete="off" spellCheck={false} />
            </Field>
          </>
        ) : null}
        {createDatabase.isError ? <ErrorBlock live compact error={createDatabase.error} title="The database was not created" /> : null}
      </form>
    </Dialog>
  );
}

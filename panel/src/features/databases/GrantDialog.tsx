import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import type { SyntheticEvent } from "react";

import type { DatabaseUser } from "../../api/queries/databases";
import { databasesQuery } from "../../api/queries/databases";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { useDatabaseActions } from "./useDatabaseActions";

export type GrantMode = "grant" | "revoke";

export interface GrantDialogProps {
  mode: GrantMode;
  user: DatabaseUser;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Grants or revokes privileges on one database for one user. Privileges are free text (WASM
 * exposes no endpoint listing an engine's valid privilege vocabulary): left blank, the engine
 * grants or revokes its own default (every privilege); the server refuses anything not on its
 * own whitelist and says so verbatim.
 */
export function GrantDialog({ mode, user, open, onOpenChange }: GrantDialogProps) {
  const formId = useId();
  const databases = useQuery({ ...databasesQuery(user.engine), enabled: open });
  const [database, setDatabase] = useState("");
  const [privileges, setPrivileges] = useState("");
  const [host, setHost] = useState(user.host);
  const { grant, revoke } = useDatabaseActions();
  const action = mode === "grant" ? grant : revoke;

  const close = (next: boolean): void => {
    if (!next && action.isPending) return;
    onOpenChange(next);
    if (!next) {
      setDatabase("");
      setPrivileges("");
      action.reset();
    }
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (database === "") return;
    const list = privileges
      .split(",")
      .map((value) => value.trim().toUpperCase())
      .filter((value) => value !== "");
    action.mutate(
      { engine: user.engine, username: user.username, database, host, privileges: list },
      { onSuccess: () => close(false) },
    );
  };

  const options = databases.data?.databases.map((item) => ({ value: item.name, label: item.name })) ?? [];

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={mode === "grant" ? `Grant privileges to ${user.username}` : `Revoke privileges from ${user.username}`}
      description={
        mode === "grant"
          ? "Grants access on one database. Leave privileges blank for every privilege."
          : "Revokes access on one database. Leave privileges blank to revoke every privilege."
      }
      footer={
        <>
          <Button disabled={action.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button
            type="submit"
            form={formId}
            variant={mode === "revoke" ? "danger" : "primary"}
            loading={action.isPending}
            disabled={database === ""}
          >
            {mode === "grant" ? "Grant" : "Revoke"}
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="flex flex-col gap-4">
        <Field label="Database" nativeLabel={false}>
          <Select
            aria-label="Database"
            value={database}
            onValueChange={setDatabase}
            placeholder={databases.isPending ? "Loading databases..." : "Choose a database"}
            options={options}
            disabled={databases.isPending || options.length === 0}
          />
        </Field>
        <Field label="Privileges" optional description="Comma-separated, in the engine's own words: SELECT, INSERT, ALL PRIVILEGES.">
          <Input mono value={privileges} onValueChange={setPrivileges} autoComplete="off" spellCheck={false} placeholder="ALL PRIVILEGES" />
        </Field>
        <Field label="Host">
          <Input mono value={host} onValueChange={setHost} autoComplete="off" spellCheck={false} />
        </Field>
        {action.isError ? (
          <ErrorBlock live compact error={action.error} title={mode === "grant" ? "The grant failed" : "The revoke failed"} />
        ) : null}
      </form>
    </Dialog>
  );
}

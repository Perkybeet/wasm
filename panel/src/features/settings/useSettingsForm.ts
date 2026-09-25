import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { SyntheticEvent } from "react";

import { splitErrors } from "./formErrors";

export type FormValues = Record<string, string | boolean>;

export interface SettingsFormOptions<V extends FormValues> {
  /** What the server holds now, as form values; undefined while it loads. */
  server: V | undefined;
  /** The fields, named as the API names them, so a 422's `fields` lands beside the right one. */
  names: readonly (keyof V & string)[];
  /** The form's only field: a refusal that names no field is then about it. */
  soleField?: keyof V & string;
  /**
   * Writes the values. It resolves once the server accepted them and the cached answer holds
   * the saved values, so the form never flashes back to the old ones.
   */
  save: (values: V) => Promise<void>;
}

export interface SettingsForm<V extends FormValues> {
  values: V | undefined;
  set: <K extends keyof V & string>(name: K, value: V[K]) => void;
  /** Fields whose value differs from the server's, in form order. */
  changed: readonly (keyof V & string)[];
  dirty: boolean;
  pending: boolean;
  fieldErrors: Partial<Record<keyof V & string, string>>;
  formError: unknown;
  submit: (event: SyntheticEvent<HTMLFormElement>) => void;
  discard: () => void;
}

/**
 * One section of settings as a form: the operator's edits over the server's values, what is
 * dirty, the save, and the server's verdict placed beside each field. The server is the one
 * that validates; nothing here second-guesses it.
 */
export function useSettingsForm<V extends FormValues>({ server, names, soleField, save }: SettingsFormOptions<V>): SettingsForm<V> {
  const [draft, setDraft] = useState<Partial<V>>({});
  // A field's error is hidden once it is edited: the message is about the value that was sent.
  const [edited, setEdited] = useState<ReadonlySet<string>>(new Set());
  const mutation = useMutation({
    mutationFn: save,
    onSuccess: () => {
      setDraft({});
    },
    onSettled: () => {
      setEdited(new Set());
    },
  });

  const values: V | undefined = server === undefined ? undefined : { ...server, ...draft };
  const changed = server === undefined ? [] : names.filter((name) => name in draft && draft[name] !== server[name]);
  const split = splitErrors(mutation.error, names, soleField);
  const fieldErrors: Partial<Record<keyof V & string, string>> = {};
  for (const name of names) {
    const message = split.fields[name];
    if (message !== undefined && !edited.has(name)) fieldErrors[name] = message;
  }

  return {
    values,
    set: (name, value) => {
      setDraft((current) => ({ ...current, [name]: value }));
      setEdited((current) => new Set([...current, name]));
    },
    changed,
    dirty: changed.length > 0,
    pending: mutation.isPending,
    fieldErrors,
    formError: split.form,
    submit: (event) => {
      event.preventDefault();
      if (values === undefined || mutation.isPending || changed.length === 0) return;
      mutation.mutate(values);
    },
    discard: () => {
      setDraft({});
      setEdited(new Set());
      mutation.reset();
    },
  };
}

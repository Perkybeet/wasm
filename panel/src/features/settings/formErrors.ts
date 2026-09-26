/**
 * Where a failed save is shown: next to the field it is about, or above the form.
 *
 * The API names the failing fields of a 422 in `fields` (keyed by the model's field name), and
 * the console never re-validates what the server already rules on: the message beside a field
 * is the server's, verbatim. A refusal that names no field (a 400 from the configuration's own
 * rules, such as "apps_directory must be an absolute path") belongs to the form - unless the
 * form has one field, where there is no doubt which one it is about.
 */

import { isApiError } from "../../api/client";

export interface SplitErrors<K extends string> {
  /** A message per field of this form, as the server worded it. */
  fields: Partial<Record<K, string>>;
  /** What could not be pinned to a field: shown above the form, verbatim, with its fix. */
  form: unknown;
}

const NONE = { fields: {}, form: null } as const;

/** One line for beside a field: the server's words, then its fix when it gave one. */
function sentence(detail: string, hint: string | null): string {
  return hint === null ? detail : `${detail} ${hint}`;
}

/**
 * Splits a failed save into per-field messages and a form-level error.
 *
 * @param error What the save threw.
 * @param names The fields this form shows, by the name the API uses for them.
 * @param soleField The form's only field, which a field-less refusal is then about.
 */
export function splitErrors<K extends string>(error: unknown, names: readonly K[], soleField?: K): SplitErrors<K> {
  if (error === null || error === undefined) return NONE;
  if (!isApiError(error)) return { fields: {}, form: error };

  const known = new Set<string>(names);
  const fields: Partial<Record<K, string>> = {};
  let unplaced = false;
  for (const [name, message] of Object.entries(error.fields ?? {})) {
    if (known.has(name)) fields[name as K] = message;
    else unplaced = true;
  }
  if (Object.keys(fields).length > 0) return { fields, form: unplaced ? error : null };

  // Refusals that are not about a value: nothing to put beside a field.
  const aboutTheValue = error.status === 400 || error.status === 422;
  if (soleField !== undefined && aboutTheValue) {
    return { fields: { [soleField]: sentence(error.detail, error.hint) } as Partial<Record<K, string>>, form: null };
  }
  return { fields: {}, form: error };
}

/**
 * `splitErrors`, plus the configuration's own refusals placed beside their field.
 *
 * A typed endpoint that writes through `Config.set` answers a value the configuration's rules
 * refuse as a 400 with no `fields`, in the words `wasm config set` uses, which always begin
 * with the dotted key: "monitor.smtp.host is not a valid hostname: ...". That key names the
 * field as surely as `fields` would, so the message goes beside it, verbatim, instead of
 * above the form.
 *
 * @param keys Dotted configuration key to the form field it is about.
 */
export function splitConfigErrors<K extends string>(
  error: unknown,
  names: readonly K[],
  keys: Readonly<Record<string, K>>,
): SplitErrors<K> {
  const split = splitErrors(error, names);
  const refusal = split.form;
  if (!isApiError(refusal) || refusal.status !== 400 || refusal.fields !== null) return split;
  // Longest first, so "monitor.smtp.port" is not claimed by a shorter key it starts with.
  const key = Object.keys(keys)
    .sort((a, b) => b.length - a.length)
    .find((candidate) => refusal.detail.startsWith(`${candidate} `));
  const field = key === undefined ? undefined : keys[key];
  if (field === undefined) return split;
  return { fields: { ...split.fields, [field]: sentence(refusal.detail, refusal.hint) }, form: null };
}

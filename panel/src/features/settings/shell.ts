/**
 * The terminal side of a setting: the `wasm config` command that does what a form does, so an
 * operator who lives in a shell can script the same change (D6, parity).
 */

/** Characters a POSIX shell passes through unquoted. */
const SAFE = /^[A-Za-z0-9_@%+=:,./-]+$/;

/** Quotes a value for a POSIX shell: bare when safe, otherwise in single quotes. */
export function shellQuote(value: string): string {
  if (value !== "" && SAFE.test(value)) return value;
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

/** `wasm config set <key> <value>`, the value quoted for the shell. */
export function configSetCommand(key: string, value: string | number | boolean): string {
  return `wasm config set ${key} ${shellQuote(String(value))}`;
}

/** `wasm config get <key>`. */
export function configGetCommand(key: string): string {
  return `wasm config get ${key}`;
}

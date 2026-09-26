/**
 * The `.env` grammar, exactly as WASM reads the file on disk.
 *
 * `EnvManager.read_env_file` (src/wasm/deployers/helpers/env_manager.py) is the one reader
 * of an app's environment: `wasm env show`, the API and the deploy all go through it. What
 * the operator pastes here is parsed the same way, character for character, so the preview
 * they approve is what WASM will read back. `tests/fixtures/env/*.env` pins it: the Python
 * suite parses each fixture with EnvManager, this module's test parses the same files, and
 * both compare with the `.json` beside them.
 *
 * The grammar, in Python's terms: the text is read with universal newlines (`\r\n` and a
 * lone `\r` become `\n`), split with `str.splitlines()`, and each line `str.strip()`ped.
 * Blank lines and lines starting with `#` are skipped, so is a line without `=`. The line is
 * split at its first `=`; name and value are stripped again, a leading `export` keyword (the
 * lowercase word and the spaces or tabs after it) is dropped from the name the way a shell
 * sourcing the file would, and one pair of matching quotes (`"..."` or `'...'`) around the value
 * is removed. Nothing else: no escapes, no inline comments. A later line for the same name
 * replaces the value and keeps the name's first position.
 *
 * The writer quotes a value only when writing it bare would change how it reads back
 * (surrounding spaces, or a value that is itself wrapped in a pair of quotes), so every value
 * the API accepts is read back exactly as it was saved.
 */

/** Characters Python's `str.isspace()` is true for: what `str.strip()` removes. */
const PYTHON_SPACE = new Set([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003,
  0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
]);

/** What `str.splitlines()` splits on, once universal newlines have turned `\r` into `\n`. */
// eslint-disable-next-line no-control-regex -- Python splits lines on these control characters too
const PYTHON_LINE_BREAK = /[\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]/;

/** `str.strip()`: JavaScript's trim() strips a different set (it takes U+FEFF, leaves U+001C). */
export function pythonStrip(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && PYTHON_SPACE.has(text.charCodeAt(start))) start += 1;
  while (end > start && PYTHON_SPACE.has(text.charCodeAt(end - 1))) end -= 1;
  return text.slice(start, end);
}

function unquote(value: string): string {
  if (value.length < 2) return value;
  const first = value[0];
  const last = value[value.length - 1];
  return (first === '"' && last === '"') || (first === "'" && last === "'") ? value.slice(1, -1) : value;
}

/** `EnvManager._EXPORT_PREFIX`: `export` and at least one space or tab, consumed whole. */
const EXPORT_PREFIX = /^export[ \t]+(.*)$/s;

function withoutExport(name: string): string {
  return EXPORT_PREFIX.exec(name)?.[1] ?? name;
}

export interface ParsedLine {
  /** 1-based line number, counted the way the parser splits lines. */
  line: number;
  name: string;
  value: string;
}

export interface ParsedDotenv {
  /** Every variable, in the order its name first appeared, with its last value. */
  variables: Map<string, string>;
  /** Every assignment line, in order, duplicates included. */
  assignments: ParsedLine[];
  /** Lines that were neither blank, a comment nor an assignment: skipped, as WASM skips them. */
  skipped: ParsedLine[];
}

/** Splits text into lines the way Python reads and splits a file. */
export function pythonLines(text: string): string[] {
  const lines = text.replace(/\r\n?/g, "\n").split(PYTHON_LINE_BREAK);
  // splitlines() yields no empty last element after a final line break.
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/** Parses a `.env` file exactly as `EnvManager.read_env_file` does. */
export function parseDotenv(text: string): ParsedDotenv {
  const variables = new Map<string, string>();
  const assignments: ParsedLine[] = [];
  const skipped: ParsedLine[] = [];
  pythonLines(text).forEach((raw, index) => {
    const line = pythonStrip(raw);
    if (line === "" || line.startsWith("#")) return;
    const at = line.indexOf("=");
    if (at === -1) {
      skipped.push({ line: index + 1, name: line, value: "" });
      return;
    }
    const name = withoutExport(pythonStrip(line.slice(0, at)));
    const value = unquote(pythonStrip(line.slice(at + 1)));
    variables.set(name, value);
    assignments.push({ line: index + 1, name, value });
  });
  return { variables, assignments, skipped };
}

// ---------------------------------------------------------------------------------------
// What the API accepts (wasm/validators/environment.py), checked before sending.

/** ENV_NAME_PATTERN: a POSIX environment identifier. */
const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** _FORBIDDEN_VALUE_CHARS: C0 control characters and DEL end or corrupt a unit directive. */
// eslint-disable-next-line no-control-regex -- the control characters the API refuses are the point
const FORBIDDEN = /[\x00-\x1f\x7f]/;

const CONTROL_NAMES: Readonly<Record<string, string>> = {
  "\n": "a newline",
  "\r": "a carriage return",
  "\t": "a tab",
  "\x00": "a NUL character",
};

export function isValidName(name: string): boolean {
  return NAME.test(name);
}

/** Why the API would refuse this name, or null when it accepts it. */
export function nameProblem(name: string): string | null {
  if (isValidName(name)) return null;
  if (name === "") return "A line has a value but no name before the =.";
  return `"${name}" is not a variable name. Names start with a letter or underscore and hold only letters, digits and underscores.`;
}

/** Why the API would refuse this value, or null when it accepts it. */
export function valueProblem(value: string): string | null {
  const match = FORBIDDEN.exec(value);
  if (match === null) return null;
  const char = match[0];
  const code = char.charCodeAt(0).toString(16).toUpperCase().padStart(4, "0");
  return `The value contains ${CONTROL_NAMES[char] ?? `the control character U+${code}`}, which a systemd unit cannot hold.`;
}

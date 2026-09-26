/**
 * Reading a web server's refusal: the backend answers a configuration test that failed with
 * a 400 whose `detail` says which server refused it and whose `output` carries the server's
 * own output, verbatim (`ValidationError.output`). Pure.
 */

import { isApiError } from "../../api/client";

export interface ConfigRejection {
  /** The backend's sentence: "nginx rejected the configuration for example.com". */
  summary: string;
  /** What the server printed, verbatim. */
  output: string;
  /** The line of the edited file the server points at, when it names one. */
  line: number | null;
}

/**
 * The line a test failure points at: nginx ends its message with `in <file>:<line>`, Apache
 * says `Syntax error on line <line> of <file>`.
 */
export function failingLine(output: string): number | null {
  for (const text of output.split("\n")) {
    const nginx = /\[(?:emerg|crit|alert)\].* in \S+:(\d+)\s*$/.exec(text);
    if (nginx?.[1] !== undefined) return Number(nginx[1]);
    const apache = /Syntax error on line (\d+) of /.exec(text);
    if (apache?.[1] !== undefined) return Number(apache[1]);
  }
  return null;
}

/** The web server's refusal in an API error, or null for any other failure. */
export function configRejection(error: unknown): ConfigRejection | null {
  if (!isApiError(error) || error.status !== 400 || error.error !== "validationerror") return null;
  const output = error.output ?? "";
  return { summary: error.detail, output, line: failingLine(output) };
}

/** Character offset of the start of a 1-based line, for moving the caret there. */
export function lineOffset(text: string, line: number): number {
  let offset = 0;
  for (let current = 1; current < line; current += 1) {
    const next = text.indexOf("\n", offset);
    if (next === -1) return text.length;
    offset = next + 1;
  }
  return offset;
}

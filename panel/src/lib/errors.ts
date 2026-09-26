/**
 * The parts of an error as the console shows it: the suggested fix above, the system's own
 * words below, never paraphrased, and - when the failing tool printed something of its own
 * (psql's, git's or nginx's own text) - that output verbatim, separate from the one-line
 * `detail`.
 */
export interface DescribedError {
  hint: string | null;
  detail: string;
  /** A failing tool's own output, verbatim, when the error carries one. Never paraphrase it. */
  output: string | null;
}

function field(value: unknown, key: string): string | null {
  if (typeof value !== "object" || value === null || !(key in value)) return null;
  const found: unknown = (value as Record<string, unknown>)[key];
  return typeof found === "string" && found !== "" ? found : null;
}

/**
 * Extracts hint, verbatim detail and any tool output from anything thrown. API errors carry
 * `detail`, `hint` and `output` (the backend error contract); plain errors carry a message.
 */
export function describeError(error: unknown): DescribedError {
  const detail = field(error, "detail") ?? field(error, "message") ?? String(error);
  return { hint: field(error, "hint"), detail, output: field(error, "output") };
}

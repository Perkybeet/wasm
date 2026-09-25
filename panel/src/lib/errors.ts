/**
 * The two halves of an error as the console shows it: the suggested fix above, the system's
 * own words below, never paraphrased.
 */
export interface DescribedError {
  hint: string | null;
  detail: string;
}

function field(value: unknown, key: string): string | null {
  if (typeof value !== "object" || value === null || !(key in value)) return null;
  const found: unknown = (value as Record<string, unknown>)[key];
  return typeof found === "string" && found !== "" ? found : null;
}

/**
 * Extracts hint and verbatim detail from anything thrown. API errors carry `detail` and
 * `hint` (the backend error contract); plain errors carry a message.
 */
export function describeError(error: unknown): DescribedError {
  const detail = field(error, "detail") ?? field(error, "message") ?? String(error);
  return { hint: field(error, "hint"), detail };
}

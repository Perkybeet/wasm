/**
 * Domain names as the operator types them. The server is the authority on what it accepts
 * (`strict_domain`); this only catches the obvious before a round trip, and says so in the
 * same terms.
 */

const LABEL = /^(?!-)[a-z0-9-]{1,63}(?<!-)$/;
const TLD = /^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$/;

/** Trims and lowercases, the two changes the server accepts silently. */
export function normalizeDomain(value: string): string {
  return value.trim().toLowerCase();
}

/**
 * Why a name is not a domain the server would take, or null when it looks like one.
 * Schemes, ports and paths are refused rather than stripped: the server refuses them too.
 */
export function domainProblem(value: string): string | null {
  const name = normalizeDomain(value);
  if (name === "") return "Enter a domain, such as app.example.com.";
  if (/^[a-z][a-z0-9+.-]*:\/\//.test(name)) return "Enter the bare domain, without http:// or https://.";
  if (/[/:?#@\s]/.test(name)) return "Enter the bare domain: no path, port or spaces.";
  if (name.length > 253) return "A domain is at most 253 characters long.";
  const labels = name.split(".");
  if (labels.length < 2) return "A domain has at least two parts, such as example.com.";
  if (labels.some((label) => label === "")) return "A domain cannot have empty parts or start or end with a dot.";
  if (!labels.every((label) => LABEL.test(label))) {
    return "Each part of a domain uses letters, digits and hyphens, and cannot start or end with a hyphen.";
  }
  if (!TLD.test(labels.at(-1) ?? "")) return "The last part of a domain is letters only, such as .com or .es.";
  return null;
}

/**
 * The names in free text: separated by spaces, commas or new lines, lowercased, each once,
 * in the order typed.
 */
export function parseNames(text: string): string[] {
  const seen = new Set<string>();
  for (const part of text.split(/[\s,;]+/)) {
    const name = normalizeDomain(part);
    if (name !== "") seen.add(name);
  }
  return [...seen];
}

/** The `www.` twin of a name, or null for a name that already is one. */
export function wwwOf(domain: string): string | null {
  const name = normalizeDomain(domain);
  return name.startsWith("www.") ? null : `www.${name}`;
}

/**
 * A list of names for a dense cell: the first `max`, joined, and how many more there are.
 * The full list belongs in a tooltip; this only decides what fits inline.
 */
export function truncatedNames(names: readonly string[], max = 3): { shown: string; rest: number } {
  const shown = names.slice(0, max);
  return { shown: shown.join(", "), rest: names.length - shown.length };
}

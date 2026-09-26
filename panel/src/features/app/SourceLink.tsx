import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";

/**
 * The repository or path an app is deployed from: a link when a browser can open it (an HTTPS
 * URL, not a local path or an SSH remote), in the accent like every other link out, with the
 * external mark; otherwise the value itself. Null stays null, for the fact list to say so.
 */
export function sourceLink(source: string | null): ReactNode {
  if (source === null) return null;
  if (!source.startsWith("https://")) return source;
  return (
    <a
      href={source}
      target="_blank"
      rel="noreferrer"
      translate="no"
      className="inline-flex max-w-full min-w-0 items-center gap-1 rounded-[4px] text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
    >
      <span className="truncate">{source}</span>
      <ExternalLink aria-hidden="true" className="size-3.5 shrink-0" />
      <span className="sr-only"> (opens in a new tab)</span>
    </a>
  );
}

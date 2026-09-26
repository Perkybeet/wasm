import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";

import { isHttpUrl } from "../../lib/url";

/**
 * A source the server redacted (`public_source` in `wasm.web.api.apps`): every credential in
 * a clone URL - `user:***@host`, or a bare token as `***@host` - becomes this literal marker.
 * The exact substring `redact_url_credentials` and its userinfo-only counterpart both leave
 * right before the host, in both forms.
 */
const MASKED_CREDENTIAL = "***@";

/**
 * The repository or path an app is deployed from: a link when a browser can open it (an
 * http(s) URL, not a local path, an SSH remote, or one with a masked credential - clicking
 * `https://***@github.com/...` would offer a browser sign-in prompt over a placeholder that
 * opens nothing), in the accent like every other link out, with the external mark; otherwise
 * the value itself. Null stays null, for the fact list to say so.
 */
export function sourceLink(source: string | null): ReactNode {
  if (source === null) return null;
  if (!isHttpUrl(source) || source.includes(MASKED_CREDENTIAL)) return source;
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

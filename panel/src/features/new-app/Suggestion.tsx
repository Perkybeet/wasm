import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

/** `wasm setup init` in a sentence becomes code; the rest stays text. */
function inline(text: string): ReactNode[] {
  return text.split("`").map((part, index) =>
    index % 2 === 1 ? (
      <code key={index} translate="no" className="mono text-12 text-fg">
        {part}
      </code>
    ) : (
      part
    ),
  );
}

/**
 * What the backend suggests doing, as it wrote it. Its paragraphs are separated by a blank
 * line, and a paragraph of several lines is a file to write (the smallest compose.yaml that
 * builds a Dockerfile), so it is shown as one, verbatim, in mono: indentation is meaning there.
 */
export function Suggestion({ text, className }: { text: string; className?: string }) {
  const paragraphs = text.split(/\n\s*\n/).filter((paragraph) => paragraph.trim() !== "");
  return (
    <div className={cx("flex min-w-0 flex-col gap-2 text-13 text-pretty text-fg-muted", className)}>
      {paragraphs.map((paragraph, index) =>
        paragraph.includes("\n") ? (
          <pre
            key={index}
            translate="no"
            className="mono rounded-control border border-border bg-bg-sunken px-3 py-2 text-12 break-words whitespace-pre-wrap text-fg"
          >
            {paragraph}
          </pre>
        ) : (
          <p key={index}>{inline(paragraph)}</p>
        ),
      )}
    </div>
  );
}

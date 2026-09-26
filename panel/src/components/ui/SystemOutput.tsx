import { useRef } from "react";

import { cx } from "../../lib/cx";
import { useNeedsScrollFocus } from "./scrollable";

export interface SystemOutputProps {
  /** The system's own words, shown verbatim: never paraphrased, never trimmed. */
  children: string;
  /**
   * What the output is, as a screen reader announces it when it takes focus: "What nginx -t
   * said", "Output of the failed deploy". Used only while the output scrolls.
   */
  label: string;
  /** The height it scrolls beyond, as a Tailwind max-height class. */
  maxHeight?: string;
  className?: string;
}

/**
 * Output of a program, verbatim in mono: an error's detail, a test's report. Tall output scrolls
 * inside its box, and only then does the box join the tab order (with a name), so the keyboard
 * can scroll it; output that fits adds no stop.
 */
export function SystemOutput({ children, label, maxHeight = "max-h-48", className }: SystemOutputProps) {
  const ref = useRef<HTMLPreElement>(null);
  const scrolls = useNeedsScrollFocus(ref);
  return (
    <pre
      ref={ref}
      translate="no"
      {...(scrolls ? { tabIndex: 0, role: "region", "aria-label": label } : {})}
      className={cx(
        "overflow-auto text-12 whitespace-pre-wrap break-words text-fg scroll-thin",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        maxHeight,
        className,
      )}
    >
      {children}
    </pre>
  );
}

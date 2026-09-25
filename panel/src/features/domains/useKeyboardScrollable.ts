import { useLayoutEffect } from "react";
import type { RefObject } from "react";

/**
 * Makes the system output under an element reachable from the keyboard when it scrolls: a
 * `<pre>` taller than its box can only be read by scrolling it, and a keyboard scrolls what has
 * focus (WCAG 2.1.1; axe's scrollable-region-focusable).
 *
 * A stopgap for ErrorBlock, whose `<pre>` lives in the page kit: the fix belongs there (a
 * tabIndex on the output when it overflows), and this goes away with it.
 */
export function useKeyboardScrollable(ref: RefObject<HTMLElement | null>): void {
  useLayoutEffect(() => {
    for (const output of ref.current?.querySelectorAll("pre") ?? []) {
      if (output.scrollHeight > output.clientHeight && !output.hasAttribute("tabindex")) output.tabIndex = 0;
    }
  });
}

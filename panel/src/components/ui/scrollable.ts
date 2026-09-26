import { useLayoutEffect, useState } from "react";
import type { RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), [contenteditable]:not([contenteditable="false"])';

/** Whether the element's content is larger than its box, either way. */
function overflows(element: HTMLElement): boolean {
  return element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1;
}

/**
 * Whether a scrolling box has to take focus itself for a keyboard to scroll it.
 *
 * A keyboard scrolls what has focus. A box whose content overflows and holds nothing focusable
 * (system output in a `<pre>`, a long read-only dialog body) cannot be read past its first
 * screen without a mouse (WCAG 2.1.1; axe's scrollable-region-focusable). A box that does not
 * scroll, or whose controls take focus and bring the scroll with them, must not add a stop to
 * the tab order: this says which case the box is in, re-checked whenever its content or its
 * size changes.
 *
 * Spread the answer as `tabIndex={0}` plus a role and a name, so the stop is announced as what
 * it is: see SystemOutput.
 */
export function useNeedsScrollFocus(ref: RefObject<HTMLElement | null>): boolean {
  const [needs, setNeeds] = useState(false);

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const check = (): void => {
      const next = overflows(element) && element.querySelector(FOCUSABLE) === null;
      setNeeds((current) => (current === next ? current : next));
    };
    check();
    // New text inside changes the content without changing the box; a narrower box wraps the
    // same text onto more lines. Either can start or stop the scrolling.
    const content = new MutationObserver(check);
    content.observe(element, { childList: true, characterData: true, subtree: true });
    const size = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(check);
    size?.observe(element);
    return () => {
      content.disconnect();
      size?.disconnect();
    };
  }, [ref]);

  return needs;
}

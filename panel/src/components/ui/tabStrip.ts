import { useLayoutEffect } from "react";
import type { RefObject } from "react";

/** Which ends of a horizontal strip hide content: what the edge fade shows. */
export type StripOverflow = "none" | "start" | "end" | "both";

/** Pixels of slack before an end counts as hiding something: sub-pixel widths round. */
const SLACK = 2;

export function stripOverflow(scrollLeft: number, clientWidth: number, scrollWidth: number): StripOverflow {
  const start = scrollLeft > SLACK;
  const end = scrollLeft + clientWidth < scrollWidth - SLACK;
  return start && end ? "both" : start ? "start" : end ? "end" : "none";
}

/**
 * How far to scroll a strip so a tab sits in the middle of it, or null when the tab is already
 * fully in view (nothing moves then: a tab the operator can see is never pulled away).
 */
export function scrollToCentre(strip: DOMRect, tab: DOMRect, scrollLeft: number): number | null {
  if (tab.left >= strip.left && tab.right <= strip.right) return null;
  return Math.max(0, scrollLeft + (tab.left - strip.left) - (strip.width - tab.width) / 2);
}

/**
 * A row of tabs wider than the screen, on a phone: the active tab is brought into view (on
 * load, deep-linked to the seventh tab, it would otherwise be off to the right) and each end
 * that hides tabs says so with a fade (`data-overflow`, styled by `.tab-strip`).
 *
 * @param ref The scrolling strip.
 * @param active Selector of the active tab inside it, e.g. `[data-active]`.
 * @param attribute The attribute that moves when the selection changes, watched to follow it.
 */
export function useTabStrip(ref: RefObject<HTMLElement | null>, active: string, attribute: string): void {
  useLayoutEffect(() => {
    const strip = ref.current;
    if (!strip) return;

    const mark = (): void => {
      strip.dataset["overflow"] = stripOverflow(strip.scrollLeft, strip.clientWidth, strip.scrollWidth);
    };
    const reveal = (): void => {
      const tab = strip.querySelector<HTMLElement>(active);
      if (tab) {
        const left = scrollToCentre(strip.getBoundingClientRect(), tab.getBoundingClientRect(), strip.scrollLeft);
        if (left !== null) strip.scrollLeft = left;
      }
      mark();
    };

    reveal();
    strip.addEventListener("scroll", mark, { passive: true });
    const selection = new MutationObserver(reveal);
    selection.observe(strip, { attributes: true, attributeFilter: [attribute], subtree: true });
    const size = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(reveal);
    size?.observe(strip);
    return () => {
      strip.removeEventListener("scroll", mark);
      selection.disconnect();
      size?.disconnect();
    };
  }, [ref, active, attribute]);
}

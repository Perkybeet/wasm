import { useRef, useSyncExternalStore } from "react";

/**
 * One clock for every live time label on screen. A single interval ticks once a second while
 * anything listens and stops when nothing does; each label re-renders only when its own text
 * would change (see `useNow`), so fifty "3m ago" labels cost one timer and almost no renders.
 */

let now = Date.now();
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;

function tick(): void {
  now = Date.now();
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    // The clock may have been stopped for a while: refresh it now. React re-reads the
    // snapshot right after subscribing and re-renders before paint if it moved, so a label
    // never shows a time computed from a stale clock.
    now = Date.now();
    timer = setInterval(tick, 1_000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

/**
 * The current time in milliseconds, refreshed once per step rather than once per tick: the
 * value only moves when a step boundary is crossed, so the component re-renders that often.
 * `stepFor` picks the step from the current time, so a label can slow down as what it
 * describes grows older.
 */
export function useNow(stepFor: (current: number) => number): number {
  const last = useRef<{ bucket: number; at: number } | null>(null);
  return useSyncExternalStore(subscribe, () => {
    const step = Math.max(1_000, stepFor(now));
    const bucket = Math.floor(now / step);
    // The time at which the bucket was entered, not the bucket's start: the text computed from
    // it is exact when drawn, and stays until it would read differently.
    if (last.current?.bucket !== bucket) last.current = { bucket, at: now };
    return last.current.at;
  });
}

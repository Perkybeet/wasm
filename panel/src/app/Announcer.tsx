import { useEffect, useState } from "react";

export type Politeness = "polite" | "assertive";

type Listener = (message: string, politeness: Politeness) => void;

const listeners = new Set<Listener>();

/**
 * Says something to screen reader users without moving focus. Polite for transitions (a
 * deploy started, a page loaded), assertive for failures. Never for log lines: a stream of
 * them would drown out everything else.
 */
export function announce(message: string, politeness: Politeness = "polite"): void {
  for (const listener of listeners) listener(message, politeness);
}

export function useAnnounce(): (message: string, politeness?: Politeness) => void {
  return announce;
}

// Cleared first and set a beat later, so the same sentence twice is still read twice; cleared
// again after a while, so stale text does not linger at the end of the document.
const SET_DELAY_MS = 100;
const CLEAR_AFTER_MS = 7_000;

/** The two live regions. Mounted once, empty, before anything is announced into them. */
export function Announcer() {
  const [polite, setPolite] = useState("");
  const [assertive, setAssertive] = useState("");

  useEffect(() => {
    const timers = new Set<ReturnType<typeof setTimeout>>();
    const later = (fn: () => void, ms: number): void => {
      const timer = setTimeout(() => {
        timers.delete(timer);
        fn();
      }, ms);
      timers.add(timer);
    };
    const listener: Listener = (message, politeness) => {
      const set = politeness === "assertive" ? setAssertive : setPolite;
      set("");
      later(() => {
        set(message);
      }, SET_DELAY_MS);
      later(() => {
        set((shown) => (shown === message ? "" : shown));
      }, CLEAR_AFTER_MS);
    };
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
      for (const timer of timers) clearTimeout(timer);
    };
  }, []);

  return (
    <div className="sr-only">
      <div role="status" aria-live="polite" aria-atomic="true" data-testid="announcer-polite">
        {polite}
      </div>
      <div role="alert" aria-live="assertive" aria-atomic="true" data-testid="announcer-assertive">
        {assertive}
      </div>
    </div>
  );
}

import { useEffect, useRef } from "react";

import { announce } from "../../app/Announcer";
import type { Politeness } from "../../app/Announcer";

/**
 * Says `message` in the live region whenever `value` changes, never for the first value: a
 * page that loads is not news, an app that goes from running to failed is. Failures pass
 * "assertive"; every other transition waits its turn.
 *
 * @param value What to watch, usually a state word; null while it is not known yet.
 * @param message What to say about the new value; null to stay silent for this one.
 */
export function useAnnounceChange(value: string | null, message: string | null, politeness: Politeness = "polite"): void {
  const previous = useRef<string | null>(null);
  useEffect(() => {
    if (value === null) return;
    const before = previous.current;
    previous.current = value;
    if (before === null || before === value || message === null) return;
    announce(message, politeness);
  }, [value, message, politeness]);
}

import { useEffect, useState } from "react";

import { copyText } from "../../lib/clipboard";

export type CopyState = "idle" | "copied" | "failed";

export interface CopyStateHandle {
  state: CopyState;
  /** Copies `value` to the clipboard and moves to "copied" or "failed"; resets after a pause. */
  copy: (value: string) => Promise<void>;
}

/**
 * The copy-and-confirm state machine every copy control shares: press, land on "copied" or
 * "failed", then rest back to "idle" so the confirmation does not linger forever. `CopyButton`
 * and `CopyTextButton` are the same behaviour behind two different looks, and this is the one
 * place it is written.
 */
export function useCopyState(resetMs = 1600): CopyStateHandle {
  const [state, setState] = useState<CopyState>("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => {
      setState("idle");
    }, resetMs);
    return () => {
      clearTimeout(timer);
    };
  }, [state, resetMs]);

  const copy = async (value: string): Promise<void> => {
    try {
      await copyText(value);
      setState("copied");
    } catch {
      // The failure is shown and announced by the caller; there is nothing else to recover.
      setState("failed");
    }
  };

  return { state, copy };
}

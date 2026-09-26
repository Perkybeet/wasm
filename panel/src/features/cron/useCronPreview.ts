import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { cronPreviewQuery } from "../../api/queries/cron";

/** How long to wait after the schedule stops changing before asking the backend to preview it. */
const DEBOUNCE_MS = 350;

/**
 * Debounces a schedule string (an alias or a raw calendar expression) and previews it through
 * `POST /api/cron/preview`, keying the query by the debounced value: react-query keeps each
 * query key's result apart, so a keystroke made obsolete by a later one can never land after
 * it and show a stale calendar - see `cronPreviewQuery`.
 */
export function useCronPreview(schedule: string) {
  const [debounced, setDebounced] = useState(schedule);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(schedule), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [schedule]);

  const trimmed = debounced.trim();
  return useQuery({ ...cronPreviewQuery(trimmed), enabled: trimmed !== "" });
}

import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { sessionQuery } from "../api/queries/auth";

interface Entry {
  title: string;
  priority: number;
}

const entries: Entry[] = [];

function apply(hostname: string | undefined): void {
  // The most specific title wins: a tab's "Logs" over its layout's domain.
  const winner = entries.reduce<Entry | null>((best, entry) => (!best || entry.priority >= best.priority ? entry : best), null);
  const parts = [winner?.title, hostname, "WASM"].filter((part): part is string => part !== undefined && part !== "");
  document.title = parts.join(" - ");
}

/**
 * Sets the browser tab's title while the calling component is mounted: the page, then the
 * machine, so an operator with several servers open can tell the tabs apart.
 *
 * @param title Null sets nothing, for a component that only sometimes owns the title.
 * @param priority Higher wins when nested views both set one (a tab inside a layout).
 */
export function useDocumentTitle(title: string | null, priority = 0): void {
  const { data: hostname } = useQuery({ ...sessionQuery(), select: (session) => session.hostname });
  useEffect(() => {
    if (title === null) return;
    const entry = { title, priority };
    entries.push(entry);
    apply(hostname);
    return () => {
      entries.splice(entries.indexOf(entry), 1);
      apply(hostname);
    };
  }, [title, priority, hostname]);
}

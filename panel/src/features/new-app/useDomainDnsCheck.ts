import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { bareDnsCheckQuery } from "../../api/queries/domains";
import type { DnsCheck } from "../../api/queries/domains";
import { domainProblem, normalizeDomain } from "../domains/names";

/** How long to wait after the domain stops changing before checking where it points. */
const DEBOUNCE_MS = 500;

export interface DomainDnsCheck {
  /** The verdict for the domain last checked, once it answers. */
  data: DnsCheck | undefined;
  isFetching: boolean;
  isError: boolean;
  error: unknown;
  /** Checks the domain typed right now, without waiting for the debounce. */
  checkNow: () => void;
}

/**
 * Where a typed domain points, checked through `GET /api/domains/dns` - the same check the
 * app Domains tab runs, before there is an application to hang the path off. Debounced while
 * the operator types, and forced at once when they move on, so the answer is never more than
 * one keystroke stale by the time it is read.
 */
export function useDomainDnsCheck(domain: string): DomainDnsCheck {
  const normalized = normalizeDomain(domain);
  const valid = domainProblem(normalized) === null;
  const [checked, setChecked] = useState(valid ? normalized : "");

  useEffect(() => {
    if (!valid) return;
    const timer = window.setTimeout(() => setChecked(normalized), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [normalized, valid]);

  const query = useQuery({ ...bareDnsCheckQuery(checked), enabled: checked !== "" });

  return {
    data: query.data,
    isFetching: query.isFetching,
    isError: query.isError,
    error: query.error,
    checkNow: () => {
      if (valid) setChecked(normalized);
    },
  };
}

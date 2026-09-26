import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type CertList = ResponseOf<"/api/certs", "get">;
export type Cert = ResponseOf<"/api/certs/{domain}", "get">;
/** One entry of the machine's list of certificates. */
export type CertEntry = CertList["certificates"][number];

export const certKeys = {
  all: ["certs"] as const,
  /** Every single certificate's own entry: the prefix of `detail`. */
  details: ["cert"] as const,
  detail: (domain: string) => ["cert", domain] as const,
};

export const certsQuery = () =>
  queryOptions({
    queryKey: certKeys.all,
    queryFn: ({ signal }) => request("get", "/api/certs", { signal }),
  });

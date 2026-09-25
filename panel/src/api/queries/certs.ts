import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type CertList = ResponseOf<"/api/certs", "get">;
export type Cert = ResponseOf<"/api/certs/{domain}", "get">;

export const certKeys = {
  all: ["certs"] as const,
  detail: (domain: string) => ["cert", domain] as const,
};

export const certsQuery = () =>
  queryOptions({
    queryKey: certKeys.all,
    queryFn: ({ signal }) => request("get", "/api/certs", { signal }),
  });

export const certQuery = (domain: string) =>
  queryOptions({
    queryKey: certKeys.detail(domain),
    queryFn: ({ signal }) => request("get", "/api/certs/{domain}", { params: { domain }, signal }),
  });

/**
 * The one read the Activity page needs that `api/queries/jobs.ts` does not define
 * (`jobKeys.log` exists there for the realtime layer's cache key shape, but no query function
 * reads it): a job's captured log, the same way `features/app/queries.ts` adds reads the
 * shared module does not cover, so as not to edit a file outside this page's ownership.
 */

import { queryOptions } from "@tanstack/react-query";

import { request } from "../../api/client";
import { jobKeys } from "../../api/queries/jobs";

export const jobLogQuery = (id: string, tail: number | null = null) =>
  queryOptions({
    queryKey: jobKeys.log(id, tail),
    queryFn: ({ signal }) =>
      request("get", "/api/jobs/{job_id}/log", {
        params: { job_id: id },
        query: tail === null ? {} : { tail },
        signal,
      }),
  });

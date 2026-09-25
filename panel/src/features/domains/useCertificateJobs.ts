import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { certKeys } from "../../api/queries/certs";
import { jobKeys, jobQuery } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";
import { siteKeys } from "../../api/queries/sites";
import { useServerEvent } from "../../realtime/events";
import { CERT_JOB_TYPES } from "./certificates";

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function isFinished(job: Pick<Job, "status">): boolean {
  return TERMINAL.has(job.status);
}

/**
 * Refreshes the certificates and the sites whenever a certificate job ends, whoever started
 * it: issuing, expanding and renewing change what certbot lists and what a site serves.
 */
export function useCertificateRefresh(): void {
  const queryClient = useQueryClient();
  useServerEvent("job", (job) => {
    if (!CERT_JOB_TYPES.has(job.type) || !isFinished(job)) return;
    void queryClient.invalidateQueries({ queryKey: certKeys.all });
    void queryClient.invalidateQueries({ queryKey: ["cert"] });
    void queryClient.invalidateQueries({ queryKey: siteKeys.all });
    void queryClient.invalidateQueries({ queryKey: ["site"] });
  });
}

export interface FollowedJob {
  /** The job being followed, as last reported; null before the first answer or when none is. */
  job: Job | null;
  /** The id being followed, known before the job itself is. */
  id: string | null;
  /** Follows a job: a snapshot from the response that queued it, or only its id. */
  follow: (job: Job | string) => void;
  /** Stops following it, and forgets how it ended. */
  dismiss: () => void;
}

/**
 * Follows one job this page queued to its end. The `job` events keep its query entry current;
 * a snapshot from the response only fills an empty entry, never overwrites a newer one.
 */
export function useFollowedJob(): FollowedJob {
  const queryClient = useQueryClient();
  const [id, setId] = useState<string | null>(null);
  const query = useQuery({
    ...jobQuery(id ?? ""),
    enabled: id !== null,
    // The events are the fast path; this is the guarantee. A fetch answered just before the
    // job ended can land after its last event and leave the entry running for good.
    refetchInterval: (entry) => (entry.state.data && isFinished(entry.state.data) ? false : 2_000),
  });
  return {
    id,
    job: id === null ? null : (query.data ?? null),
    follow: (job) => {
      if (typeof job === "string") {
        setId(job);
        return;
      }
      queryClient.setQueryData<Job>(jobKeys.detail(job.id), (current) => current ?? job);
      void queryClient.invalidateQueries({ queryKey: jobKeys.active });
      setId(job.id);
    },
    dismiss: () => {
      setId(null);
    },
  };
}

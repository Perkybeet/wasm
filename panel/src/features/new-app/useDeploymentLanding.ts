import { useQuery } from "@tanstack/react-query";

import { deploymentsQuery } from "../../api/queries/deployments";
import type { FollowedJob } from "../../api/queries/jobs";
import { isJobFinished } from "../../api/queries/jobs";

export interface LandingTarget {
  domain: string;
  jobId: string;
}

export type Landing =
  | { kind: "waiting" }
  | { kind: "deployment"; id: number }
  | { kind: "app" }
  | { kind: "failed" };

/**
 * Where to land once a deploy is queued: the deployment history row the deployer wrote for
 * this exact job, matched by `job_id` (`DeploymentOut.job_id`) - it appears, with the build
 * already streaming, well before the job function returns. `JobResponse.deployment_id` is only
 * filled from the job's result once it does (`wasm.web.jobs._execute_job`), so it would miss
 * the live log entirely if it were the only signal; it is used here only as a fallback, once
 * the job has ended, for the rare row a poll might have missed.
 *
 * A job that ends without a matching row either failed (there is nothing to show but why) or,
 * in the one case that can still happen without one, succeeded onto the application's own page.
 */
export function useDeploymentLanding(target: LandingTarget | null, followedJob: FollowedJob): Landing | null {
  const job = followedJob.job;
  const finished = job !== null && isJobFinished(job);
  const deployments = useQuery({
    ...deploymentsQuery({ domain: target?.domain ?? "", limit: 10 }),
    enabled: target !== null,
    staleTime: 0,
    // Until the row appears, or the job has ended and nothing more is coming.
    refetchInterval: (query) => {
      if (target === null) return false;
      const arrived = query.state.data?.items.some((item) => item.job_id === target.jobId);
      return arrived || finished ? false : 1_000;
    },
  });

  if (target === null) return null;
  const arrived = deployments.data?.items.find((item) => item.job_id === target.jobId);
  if (arrived !== undefined) return { kind: "deployment", id: arrived.id };
  if (!finished) return { kind: "waiting" };
  if (job.deployment_id != null) return { kind: "deployment", id: job.deployment_id };
  return job.status === "completed" ? { kind: "app" } : { kind: "failed" };
}

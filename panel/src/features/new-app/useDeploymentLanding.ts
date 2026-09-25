import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { appQuery } from "../../api/queries/apps";
import { deploymentsQuery } from "../../api/queries/deployments";
import { jobQuery } from "../../api/queries/jobs";
import type { Job } from "../../api/queries/jobs";

/** How long to wait for the deployment's history row before settling for the app's page. */
export const LANDING_PATIENCE_MS = 20_000;

export interface LandingTarget {
  domain: string;
  jobId: string;
  /** The newest deployment id of this domain before the deploy was queued (0 for none). */
  after: number;
}

export type Landing =
  | { kind: "waiting"; job: Job | null }
  | { kind: "deployment"; id: number }
  | { kind: "app" }
  | { kind: "failed"; job: Job };

const FINISHED = new Set(["completed", "failed", "cancelled"]);

/**
 * Where to land once a deploy is queued. `POST /api/apps` answers with a job id, and the
 * deployment page is keyed by the history row the deployer writes once it starts; nothing
 * links the two, so the row is recognised as the first one of this domain newer than the
 * newest before the deploy was queued. A brand-new domain has no other deploy in flight
 * (the API refuses a second app on it), so that row is this deploy's.
 *
 * When no row shows up in time the app's own page is the landing: its header follows the
 * running job. When the job fails before the deployer wrote anything, there is nowhere to
 * land, and the failure is the answer.
 */
export function useDeploymentLanding(target: LandingTarget | null): Landing | null {
  const [patienceOver, setPatienceOver] = useState(false);
  const job = useQuery({
    ...jobQuery(target?.jobId ?? ""),
    enabled: target !== null,
    refetchInterval: (entry) => (entry.state.data && FINISHED.has(entry.state.data.status) ? false : 2_000),
  });
  const ended = job.data !== undefined && FINISHED.has(job.data.status) ? job.data : null;
  const finished = ended !== null;
  const history = useQuery({
    ...deploymentsQuery({ domain: target?.domain ?? "", limit: 1 }),
    enabled: target !== null,
    staleTime: 0,
    // Until the row appears. A finished job gets one more look: the row it wrote may be newer
    // than the last poll.
    refetchInterval: (query) => {
      const newest = query.state.data?.items[0];
      return target !== null && (newest === undefined || newest.id <= target.after) ? 1_000 : false;
    },
  });
  // Asked only while still waiting: a deploy that failed before the deployer registered the
  // app has no app to ask about.
  const app = useQuery({ ...appQuery(target?.domain ?? ""), enabled: target !== null && patienceOver && !finished, retry: false });

  useEffect(() => {
    if (target === null) return;
    const timer = setTimeout(() => {
      setPatienceOver(true);
    }, LANDING_PATIENCE_MS);
    return () => {
      clearTimeout(timer);
    };
  }, [target]);

  // Once the job has ended, one more look at the history settles it: the row, if the deployer
  // wrote one, is there by then.
  const [looked, setLooked] = useState(false);
  const { refetch } = history;
  useEffect(() => {
    if (!finished) return;
    let live = true;
    void refetch().then(() => {
      if (live) setLooked(true);
    });
    return () => {
      live = false;
    };
  }, [finished, refetch]);

  if (target === null) return null;
  const newest = history.data?.items[0];
  if (newest !== undefined && newest.id > target.after) return { kind: "deployment", id: newest.id };
  if (ended !== null && looked) return ended.status === "completed" ? { kind: "app" } : { kind: "failed", job: ended };
  if (patienceOver && app.data !== undefined) return { kind: "app" };
  return { kind: "waiting", job: job.data ?? null };
}

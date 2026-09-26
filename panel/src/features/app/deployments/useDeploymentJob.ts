import { useQuery } from "@tanstack/react-query";

import type { Deployment } from "../../../api/queries/deployments";
import { jobLogQuery, jobQuery } from "../../../api/queries/jobs";
import type { Job } from "../../../api/queries/jobs";
import { parseTimestamp } from "../../../lib/format";
import { useJobStream } from "../../../realtime/sockets";
import type { SocketStatus } from "../../../realtime/sockets";
import { parseLog } from "./buildLog";

const RUNNING_DEPLOY = new Set(["queued", "running"]);

export interface JobEntry {
  text: string;
  at: Date | null;
}

export interface DeploymentJob {
  /** The console job that ran the deploy, when one did (the command line runs none). */
  job: Job | null;
  /** What the job reported, oldest first: its phases and steps. */
  entries: JobEntry[];
  /** The live connection to a running job, null when there is none to follow. */
  socket: SocketStatus | null;
}

function entriesOf(job: Job | null | undefined): JobEntry[] {
  return (job?.logs ?? []).flatMap((entry) => {
    const message = entry["message"];
    if (typeof message !== "string") return [];
    const stamp = entry["timestamp"];
    return [{ text: message, at: typeof stamp === "string" ? parseTimestamp(stamp) : null }];
  });
}

/**
 * The job behind a deployment and what it reported, from the deployment's own `job_id` - set
 * by the panel when it queued the deploy, null for one the command line or a webhook ran with
 * nothing queuing it. While the deploy runs, the job is followed over its WebSocket, which
 * carries each step as it happens; once it ended, its captured log is read instead, so the
 * phases are still there after a panel restart.
 */
export function useDeploymentJob(deployment: Deployment | undefined): DeploymentJob {
  const jobId = deployment?.job_id ?? null;
  const running = deployment !== undefined && RUNNING_DEPLOY.has(deployment.status);
  const stream = useJobStream(running ? jobId : null);
  const rest = useQuery({ ...jobQuery(jobId ?? ""), enabled: jobId !== null && !running });
  const log = useQuery({ ...jobLogQuery(jobId ?? ""), enabled: jobId !== null && !running });

  if (jobId === null) {
    return { job: null, entries: [], socket: null };
  }
  if (running) {
    return { job: stream.job, entries: entriesOf(stream.job), socket: stream.status };
  }
  const entries = log.data ? parseLog(log.data.content).map((line) => ({ text: line.text, at: line.at })) : [];
  return { job: rest.data ?? null, entries, socket: null };
}

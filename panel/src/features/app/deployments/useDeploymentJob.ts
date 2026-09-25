import { useQuery } from "@tanstack/react-query";

import type { Deployment } from "../../../api/queries/deployments";
import { activeJobsQuery, jobLogQuery, jobsQuery } from "../../../api/queries/jobs";
import type { Job } from "../../../api/queries/jobs";
import { parseTimestamp } from "../../../lib/format";
import { useJobStream } from "../../../realtime/sockets";
import type { SocketStatus } from "../../../realtime/sockets";
import { parseLog } from "./buildLog";

const RUNNING_DEPLOY = new Set(["queued", "running"]);
const RUNNING_JOB = new Set(["pending", "running"]);

/** Seconds of slack between a job's clock and the deployment row it wrote. */
const SLACK_MS = 3_000;

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

/**
 * Whether a job ran a deployment: same app, and the deployment started while the job ran. The
 * API does not link the two; at most one job runs on an app at a time, which makes the window
 * unambiguous.
 */
export function ranBy(job: Job, deployment: Deployment, now: Date = new Date()): boolean {
  if (job.metadata?.["domain"] !== deployment.domain) return false;
  const started = parseTimestamp(deployment.started_at);
  const from = parseTimestamp(job.started_at ?? job.created_at);
  if (started === null || from === null) return false;
  const to = parseTimestamp(job.completed_at) ?? now;
  return started.getTime() >= from.getTime() - SLACK_MS && started.getTime() <= to.getTime() + SLACK_MS;
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
 * The job behind a deployment and what it reported. While the deploy runs, the job is followed
 * over its WebSocket, which carries each step as it happens; once it ended, its captured log is
 * read instead, so the phases are still there after a panel restart.
 */
export function useDeploymentJob(domain: string, deployment: Deployment | undefined): DeploymentJob {
  const running = deployment !== undefined && RUNNING_DEPLOY.has(deployment.status);
  const active = useQuery({ ...activeJobsQuery(), enabled: running, refetchInterval: running ? 5_000 : false });
  const live = running
    ? (active.data?.jobs.find((job) => job.metadata?.["domain"] === domain && RUNNING_JOB.has(job.status)) ?? null)
    : null;
  const stream = useJobStream(live?.id ?? null);

  const history = useQuery({ ...jobsQuery({ domain, limit: 20 }), enabled: deployment !== undefined && !running });
  const past = !running && deployment !== undefined ? (history.data?.jobs.find((job) => ranBy(job, deployment)) ?? null) : null;
  const log = useQuery({ ...jobLogQuery(past?.id ?? ""), enabled: past !== null });

  if (running) {
    const job = stream.job ?? live;
    return { job, entries: entriesOf(job), socket: live === null ? null : stream.status };
  }
  const entries = log.data ? parseLog(log.data.content).map((line) => ({ text: line.text, at: line.at })) : [];
  return { job: past, entries, socket: null };
}

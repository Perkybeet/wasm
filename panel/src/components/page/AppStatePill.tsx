import { StatusPill } from "../ui/StatusPill";
import type { StatusPillProps } from "../ui/StatusPill";
import { appStatus, deployStatus } from "./status";

export interface AppStatePillProps extends Omit<StatusPillProps, "state" | "label"> {
  /** The status word exactly as the backend sent it; see `appStatus` for the vocabulary. */
  status: string | null | undefined;
}

/** An application's state, from whichever vocabulary the backend used to say it. */
export function AppStatePill({ status, ...rest }: AppStatePillProps) {
  const view = appStatus(status);
  return <StatusPill state={view.state} label={view.label} {...rest} />;
}

export interface DeployStatePillProps extends Omit<StatusPillProps, "state" | "label"> {
  /** A deployment's or a job's status word, as the backend sent it. */
  status: string | null | undefined;
}

/** The outcome of a deployment or a job: queued, in progress, succeeded, failed, rolled back. */
export function DeployStatePill({ status, ...rest }: DeployStatePillProps) {
  const view = deployStatus(status);
  return <StatusPill state={view.state} label={view.label} {...rest} />;
}

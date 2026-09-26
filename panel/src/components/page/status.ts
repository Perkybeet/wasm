import type { Status } from "../ui/StatusPill";

/** What the console shows for a backend state word, and whether it is a problem. */
export interface StatusView {
  /** The StatusPill state: colour and shape. */
  state: Status;
  /** The word on screen, in the backend's vocabulary. */
  label: string;
  /** True when an operator should look at it: it belongs under "Needs attention". */
  attention: boolean;
}

/**
 * Every word the backend uses for an application's state, from three sources that grew
 * separately:
 *
 * - `GET /api/apps` and the `app` event: `running`, `restarting`, `no_answer`, `stopped`,
 *   `failed`, `static`, `unknown` (resolved from systemd), and `deploying` while a deploy or
 *   update job runs;
 * - the store's `AppStatus`: `deploying`, `running`, `stopped`, `failed`, `unknown`;
 * - `wasm.core.app_state` (what `wasm list` and `wasm health` print): `Running`,
 *   `Restarting`, `No answer`, `Stopped`, `Failed`, `Static`, `Unknown`.
 *
 * Matching is case-insensitive. A stopped app is not a problem by itself (an operator stops
 * apps on purpose); a crash loop, a unit systemd gave up on, or a port nothing answers on is.
 */
const APP_STATES: Readonly<Record<string, StatusView>> = {
  running: { state: "running", label: "Running", attention: false },
  active: { state: "running", label: "Running", attention: false },
  static: { state: "static", label: "Static", attention: false },
  deploying: { state: "deploying", label: "Deploying", attention: false },
  building: { state: "deploying", label: "Building", attention: false },
  restarting: { state: "deploying", label: "Restarting", attention: true },
  activating: { state: "deploying", label: "Starting", attention: false },
  stopped: { state: "stopped", label: "Stopped", attention: false },
  inactive: { state: "stopped", label: "Stopped", attention: false },
  failed: { state: "failed", label: "Failed", attention: true },
  "no answer": { state: "failed", label: "No answer", attention: true },
  no_answer: { state: "failed", label: "No answer", attention: true },
  unknown: { state: "unknown", label: "Unknown", attention: true },
};

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Maps an application's status to what the console draws. A word the console does not know
 * is still shown, verbatim, with the unknown shape: the backend's word beats a guess.
 */
export function appStatus(status: string | null | undefined): StatusView {
  const word = status?.trim() ?? "";
  if (word === "") return { state: "unknown", label: "Unknown", attention: false };
  return APP_STATES[word.toLowerCase()] ?? { state: "unknown", label: capitalise(word), attention: false };
}

/**
 * The `DeploymentStatus` enum (`queued`, `running`, `success`, `failed`, `rolled_back`) and
 * the job statuses (`pending`, `running`, `completed`, `failed`, `cancelled`), drawn in the
 * same state language as applications.
 */
const DEPLOY_STATES: Readonly<Record<string, StatusView>> = {
  queued: { state: "deploying", label: "Queued", attention: false },
  pending: { state: "deploying", label: "Queued", attention: false },
  running: { state: "deploying", label: "In progress", attention: false },
  success: { state: "running", label: "Succeeded", attention: false },
  completed: { state: "running", label: "Succeeded", attention: false },
  failed: { state: "failed", label: "Failed", attention: true },
  rolled_back: { state: "stopped", label: "Rolled back", attention: true },
  cancelled: { state: "stopped", label: "Cancelled", attention: false },
};

/** Maps a deployment's or a job's status to what the console draws. */
export function deployStatus(status: string | null | undefined): StatusView {
  const word = status?.trim() ?? "";
  if (word === "") return { state: "unknown", label: "Unknown", attention: false };
  return (
    DEPLOY_STATES[word.toLowerCase()] ?? {
      state: "unknown",
      label: capitalise(word.replace(/_/g, " ")),
      attention: false,
    }
  );
}

/** Severity order for sorting: problems first, then work in progress, then the quiet states. */
export const STATE_RANK: Readonly<Record<Status, number>> = {
  failed: 0,
  unknown: 1,
  warning: 2,
  deploying: 3,
  running: 4,
  static: 5,
  stopped: 6,
};

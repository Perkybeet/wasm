/**
 * How deployments and releases are named on screen. The backend's words are kept where they
 * are the operator's words too (a commit, a release id); the enum values are not.
 */

import { GitPullRequestArrow, MousePointerClick, SquareTerminal } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { BadgeTone } from "../../../components/ui/Badge";

export interface TriggerWords {
  label: string;
  /** Who or what started it, for a sentence: "started by a push". */
  by: string;
  icon: LucideIcon;
}

/** `DeploymentTrigger`: what started a deploy. */
const TRIGGERS: Readonly<Record<string, TriggerWords>> = {
  webhook: { label: "Push", by: "a push to the repository", icon: GitPullRequestArrow },
  panel: { label: "Console", by: "the console", icon: MousePointerClick },
  cli: { label: "CLI", by: "the command line", icon: SquareTerminal },
};

export function triggerWords(trigger: string): TriggerWords {
  return TRIGGERS[trigger] ?? { label: trigger, by: trigger, icon: SquareTerminal };
}

export interface ReleaseBadge {
  label: string;
  tone: BadgeTone;
}

/**
 * `ReleaseStatus`, as a badge. Only the one serving and the one that failed are coloured:
 * they are the two an operator acts on; the rest are history.
 */
const RELEASE_BADGES: Readonly<Record<string, ReleaseBadge>> = {
  active: { label: "Serving", tone: "ok" },
  failed: { label: "Failed", tone: "fail" },
  rolled_back: { label: "Rolled back", tone: "neutral" },
  superseded: { label: "Previous", tone: "neutral" },
  built: { label: "Built, never served", tone: "neutral" },
};

export function releaseBadge(status: string): ReleaseBadge {
  return RELEASE_BADGES[status] ?? { label: status.replace(/_/g, " "), tone: "neutral" };
}

/** The short form of a commit, as git prints it. */
export function shortCommit(commit: string | null | undefined): string | null {
  return commit ? commit.slice(0, 7) : null;
}

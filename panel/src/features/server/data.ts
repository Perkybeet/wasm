/**
 * What the Server page reads beyond the raw API shape: the health verdict and each check's
 * status in the console's state language.
 */

import type { Status } from "../../components/ui/StatusPill";
import type { SystemHealth } from "../../api/queries/system";

export type Verdict = SystemHealth["verdict"];
export type HealthCheck = SystemHealth["checks"][number];

export interface VerdictView {
  state: Status;
  label: string;
}

/** `collect_health_report`'s verdict: "error", "warning" or "healthy" (wasm.managers.health). */
export function verdictView(verdict: string): VerdictView {
  switch (verdict) {
    case "healthy":
      return { state: "running", label: "Healthy" };
    case "warning":
      return { state: "warning", label: "Needs attention" };
    case "error":
      return { state: "failed", label: "Critical" };
    default:
      return { state: "unknown", label: verdict };
  }
}

/** One check's status: "ok", "warning", "error" or "info". */
export function checkView(status: string): VerdictView {
  switch (status) {
    case "ok":
      return { state: "running", label: "OK" };
    case "warning":
      return { state: "warning", label: "Warning" };
    case "error":
      return { state: "failed", label: "Error" };
    default:
      return { state: "unknown", label: "Info" };
  }
}

/**
 * `wasm health` names its checks in Title Case for the terminal ("Disk Space"); the console
 * writes labels in sentence case. Known names are reworded, anything else is shown as sent.
 */
const CHECK_NAMES: Readonly<Record<string, string>> = {
  "Disk Space": "Disk space",
  "SSL Certificates": "SSL certificates",
};

export function checkName(name: string): string {
  return CHECK_NAMES[name] ?? name;
}

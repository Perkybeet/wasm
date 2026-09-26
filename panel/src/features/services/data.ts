/**
 * What the Services pages read beyond the raw API shape: a unit's state in the console's
 * state language, the show-all-units toggle, and search filtering. Derived here once so the
 * list and the detail page agree.
 *
 * `GET /api/services?wasm_only=false` walks every unit on the host (`ServiceManager.
 * list_services(all_services=True)`), each flagged `managed`. A foreign unit's
 * `active_state`/`sub_state`/`result` come from systemd exactly like a WASM unit's do, so the
 * state derivation below applies to both; only the actions available to a row depend on
 * `managed`.
 */

import type { Status } from "../../components/ui/StatusPill";
import type { Service, ServiceList } from "../../api/queries/services";

export type ServiceInfo = ServiceList["services"][number];

export interface ServiceStateView {
  /** The StatusPill state: colour and shape. */
  state: Status;
  /** The word on screen. The state word itself, never systemd's result - see `detail`. */
  label: string;
  /** Systemd's own `Result` word (`exit-code`, `signal`, `timeout`, ...), shown in mono
   * beside the label. Present only when it says more than a clean `success` or nothing. */
  detail?: string;
}

/**
 * A unit's state in the console's vocabulary, from systemd's own `active_state`, `sub_state`
 * and `result` - the fields `active` alone cannot tell apart: a unit systemd is repeatedly
 * restarting spends most of its time in `activating`/`auto-restart` and reports `active:
 * false` in between attempts exactly like one stopped on purpose does.
 *
 * `active_state`/`sub_state`/`result` are optional on the wire (an older status read might
 * omit them): a unit with none of them still resolves to running or stopped from `active`
 * alone, the only distinction available.
 */
export function serviceState(
  service: Pick<ServiceInfo, "active" | "active_state" | "sub_state" | "result">,
): ServiceStateView {
  const activeState = (service.active_state ?? "").trim().toLowerCase();
  const subState = (service.sub_state ?? "").trim().toLowerCase();
  const result = (service.result ?? "").trim().toLowerCase();
  const detail = result !== "" && result !== "success" ? result : undefined;

  // Checked first: systemd reports these mid-crash-loop, before it gives up and settles on
  // ActiveState=failed, so a unit here is neither cleanly running nor cleanly stopped - a
  // problem worth a look, not work in progress, hence "warning" rather than "deploying".
  if (subState === "auto-restart" || activeState === "activating") {
    return { state: "warning", label: "Restarting" };
  }
  if (activeState === "failed") {
    return detail !== undefined ? { state: "failed", label: "Failed", detail } : { state: "failed", label: "Failed" };
  }
  if (service.active) {
    return { state: "running", label: "Running" };
  }
  return { state: "stopped", label: "Stopped" };
}

export interface ServicesSearch {
  /** Free text matched against the unit name. */
  q?: string;
  /** List every unit on the host, not just the ones WASM created (`GET ?wasm_only=false`). */
  all?: true;
}

function text(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed.slice(0, 200);
}

/** Reads the search params, dropping anything malformed instead of failing. */
export function validateServicesSearch(search: Record<string, unknown>): ServicesSearch {
  const q = text(search["q"]);
  const all = search["all"] === "1" || search["all"] === true;
  return {
    ...(q !== undefined ? { q } : {}),
    ...(all ? { all: true as const } : {}),
  };
}

export function isFiltered(search: ServicesSearch): boolean {
  return search.q !== undefined;
}

/** The services a search keeps, in their original order. */
export function filterServices(services: readonly ServiceInfo[], search: ServicesSearch): ServiceInfo[] {
  const needle = search.q?.toLowerCase();
  if (needle === undefined) return [...services];
  return services.filter((service) => {
    const haystack = `${service.name} ${service.description ?? ""}`.toLowerCase();
    return haystack.includes(needle);
  });
}

export type ServiceDetail = Service;

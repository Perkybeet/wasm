/**
 * What the Services pages read beyond the raw API shape: a unit's state in the console's
 * state language, and search filtering. Derived here once so the list and the detail page
 * agree.
 *
 * `GET /api/services` always answers `store.list_services()` regardless of the `wasm_only`
 * query parameter it declares - the endpoint never calls
 * `ServiceManager.list_services(all_services=True)`, which is the one implementation that can
 * see units WASM does not own. Every row this console can show is therefore already
 * WASM-managed: there is no way today to list a foreign unit, so "managed by WASM" is not a
 * per-row fact worth asking the backend for, it is the only thing on the page.
 *
 * The same response also has no `active_state`/`sub_state`/`result` (ServiceManager.get_status
 * reads them from systemd, but ServiceInfo does not carry them), so a crash-looping or failed
 * unit cannot be told apart from one stopped on purpose - only "active" and "stopped" exist
 * here, never "failed".
 */

import type { Status } from "../../components/ui/StatusPill";
import type { Service, ServiceList } from "../../api/queries/services";

export type ServiceInfo = ServiceList["services"][number];

/** A unit's state in the console's vocabulary. Binary: see the module docstring. */
export function serviceState(service: Pick<ServiceInfo, "active">): Status {
  return service.active ? "running" : "stopped";
}

export interface ServicesSearch {
  /** Free text matched against the unit name. */
  q?: string;
}

function text(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed.slice(0, 200);
}

/** Reads the search params, dropping anything malformed instead of failing. */
export function validateServicesSearch(search: Record<string, unknown>): ServicesSearch {
  const q = text(search["q"]);
  return q !== undefined ? { q } : {};
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

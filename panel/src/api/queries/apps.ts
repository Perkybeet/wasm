import { queryOptions } from "@tanstack/react-query";

import { isApiError, request } from "../client";
import type { ResponseOf } from "../client";

export type AppList = ResponseOf<"/api/apps", "get">;
export type App = ResponseOf<"/api/apps/{domain}", "get">;
export type AppEnv = ResponseOf<"/api/apps/{domain}/env", "get">;
export type Releases = ResponseOf<"/api/apps/{domain}/releases", "get">;
export type Release = Releases["items"][number];
export type RollbackPoints = ResponseOf<"/api/apps/{domain}/rollback-points", "get">;
export type RollbackPoint = RollbackPoints["items"][number];
export type WebhookDeliveries = ResponseOf<"/api/apps/{domain}/webhook/deliveries", "get">;
export type WebhookDelivery = WebhookDeliveries["items"][number];
export type Diagnosis = ResponseOf<"/api/apps/{domain}/diagnose", "get">;
export type MigrationPlan = ResponseOf<"/api/apps/{domain}/migrate/plan", "get">;
export type AppTypesResponse = ResponseOf<"/api/apps/types", "get">;
export type AppTypeInfo = AppTypesResponse["types"][number];

/**
 * Keys of the application queries. The list and each app are separate entries so that a
 * state change of one app (the `app` server event) patches its detail and invalidates the
 * list without refetching every other app.
 *
 * Everything read about one app lives under its own key (`["app", domain, ...]`), so a job
 * on the app finishing, which invalidates that prefix, refreshes all of it at once.
 */
export const appKeys = {
  list: ["apps"] as const,
  detail: (domain: string) => ["app", domain] as const,
  env: (domain: string, unmask: boolean) => ["app", domain, "env", { unmask }] as const,
  logs: (domain: string, lines: number) => ["app", domain, "logs", { lines }] as const,
  releases: (domain: string) => ["app", domain, "releases"] as const,
  rollbackPoints: (domain: string) => ["app", domain, "rollback-points"] as const,
  webhookDeliveries: (domain: string) => ["app", domain, "webhook-deliveries"] as const,
  diagnosis: (domain: string) => ["app", domain, "diagnosis"] as const,
  migrationPlan: (domain: string) => ["app", domain, "migration-plan"] as const,
};

export const appsQuery = () =>
  queryOptions({
    queryKey: appKeys.list,
    queryFn: ({ signal }) => request("get", "/api/apps", { signal }),
  });

export const appQuery = (domain: string) =>
  queryOptions({
    queryKey: appKeys.detail(domain),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}", { params: { domain }, signal }),
  });

/**
 * Every application type the deployer registry knows, most specific first and `auto` last -
 * the new-app wizard's one source for what "Deploy as" may offer, instead of a list hand-kept
 * in the console that a new deployer would never reach.
 */
export const appTypesQuery = () =>
  queryOptions({
    queryKey: ["app-types"] as const,
    queryFn: ({ signal }) => request("get", "/api/apps/types", { signal }),
    staleTime: 5 * 60_000,
  });

/**
 * The app's `.env`. Masked, secrets come back as the fixed `***` placeholder; unmasked needs
 * an admin credential and an elevated session, which the client asks for.
 */
export const appEnvQuery = (domain: string, unmask = false) =>
  queryOptions({
    queryKey: appKeys.env(domain, unmask),
    queryFn: ({ signal }) =>
      request("get", "/api/apps/{domain}/env", { params: { domain }, query: { unmask }, signal }),
  });

export const appLogsQuery = (domain: string, lines = 200) =>
  queryOptions({
    queryKey: appKeys.logs(domain, lines),
    queryFn: ({ signal }) =>
      request("get", "/api/apps/{domain}/logs", { params: { domain }, query: { lines }, signal }),
  });

/**
 * The app's releases. An app still deployed in place has none, and the API says so with a
 * 409; that answer is data here (`null`), not a failure, so the page can offer backups instead.
 */
export const releasesQuery = (domain: string) =>
  queryOptions({
    queryKey: appKeys.releases(domain),
    queryFn: async ({ signal }): Promise<Releases | null> => {
      try {
        return await request("get", "/api/apps/{domain}/releases", { params: { domain }, signal });
      } catch (error: unknown) {
        if (isApiError(error) && error.status === 409) return null;
        throw error;
      }
    },
  });

/** Backups the app can be rolled back to, newest first. */
export const rollbackPointsQuery = (domain: string) =>
  queryOptions({
    queryKey: appKeys.rollbackPoints(domain),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}/rollback-points", { params: { domain }, signal }),
  });

/** Deploys the app's webhook triggered, newest first. */
export const webhookDeliveriesQuery = (domain: string) =>
  queryOptions({
    queryKey: appKeys.webhookDeliveries(domain),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}/webhook/deliveries", { params: { domain }, signal }),
  });

/**
 * Why the app is or is not answering. Every probe runs on each read, so it is never cached
 * as fresh: coming back to the tab, or pressing "Run again", asks the machine again.
 */
export const diagnosisQuery = (domain: string) =>
  queryOptions({
    queryKey: appKeys.diagnosis(domain),
    queryFn: ({ signal }) => request("get", "/api/apps/{domain}/diagnose", { params: { domain }, signal }),
    staleTime: 0,
  });

/**
 * What moving an in-place app onto releases would do, or null when it is on releases already;
 * the API says so with a 409, the same way `releasesQuery` reads "no releases yet". Real, not
 * just theoretical: a finished job on this app (this migration among them, now that it runs as
 * one) invalidates the app's whole query prefix, migration-plan included, and this query can
 * still be enabled for the moment it takes the page to notice the app is on releases now.
 * Reads the disk; changes nothing.
 */
export const migrationPlanQuery = (domain: string) =>
  queryOptions({
    queryKey: appKeys.migrationPlan(domain),
    queryFn: async ({ signal }): Promise<MigrationPlan | null> => {
      try {
        return await request("get", "/api/apps/{domain}/migrate/plan", { params: { domain }, signal });
      } catch (error: unknown) {
        if (isApiError(error) && error.status === 409) return null;
        throw error;
      }
    },
    staleTime: 0,
  });

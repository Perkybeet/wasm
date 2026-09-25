import { queryOptions } from "@tanstack/react-query";

import { request } from "../client";
import type { ResponseOf } from "../client";

export type EngineList = ResponseOf<"/api/databases/engines", "get">;
export type DatabaseList = ResponseOf<"/api/databases/databases", "get">;
export type Database = ResponseOf<"/api/databases/databases/{engine}/{name}", "get">;

export const databaseKeys = {
  all: ["databases"] as const,
  engines: ["databases", "engines"] as const,
  list: (engine: string | null) => ["databases", "list", { engine }] as const,
  detail: (engine: string, name: string) => ["database", engine, name] as const,
  users: (engine: string) => ["databases", "users", engine] as const,
  backups: (engine: string | null, database: string | null) => ["databases", "backups", { engine, database }] as const,
};

export const enginesQuery = () =>
  queryOptions({
    queryKey: databaseKeys.engines,
    queryFn: ({ signal }) => request("get", "/api/databases/engines", { signal }),
  });

export const databasesQuery = (engine: string | null = null) =>
  queryOptions({
    queryKey: databaseKeys.list(engine),
    queryFn: ({ signal }) =>
      request("get", "/api/databases/databases", { query: engine === null ? {} : { engine }, signal }),
  });

export const databaseQuery = (engine: string, name: string) =>
  queryOptions({
    queryKey: databaseKeys.detail(engine, name),
    queryFn: ({ signal }) =>
      request("get", "/api/databases/databases/{engine}/{name}", { params: { engine, name }, signal }),
  });

export const databaseUsersQuery = (engine: string) =>
  queryOptions({
    queryKey: databaseKeys.users(engine),
    queryFn: ({ signal }) => request("get", "/api/databases/users/{engine}", { params: { engine }, signal }),
  });

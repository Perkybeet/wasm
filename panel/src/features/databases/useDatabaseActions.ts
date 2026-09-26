/**
 * Actions on databases, users and their privileges: one mutation per API call, matching
 * `features/apps/useAppActions.ts`. Destructive calls (drop a database, delete a user) are
 * gated by elevation on the server; the API client's "Confirm it's you" dialog handles it
 * without any special-casing here.
 */

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { request } from "../../api/client";
import { databaseKeys } from "../../api/queries/databases";
import { toast } from "../../components/ui/toast";
import { reportActionError } from "../apps/useAppActions";

export interface CreateDatabaseInput {
  engine: string;
  name: string;
  owner?: string | undefined;
  encoding?: string | undefined;
}

export interface CreateUserInput {
  engine: string;
  username: string;
  password?: string | undefined;
  database?: string | undefined;
  host: string;
}

export interface GrantInput {
  engine: string;
  username: string;
  database: string;
  host: string;
  /** Empty means the engine's own default (usually every privilege). */
  privileges: string[];
}

export interface CreateDatabaseBackupInput {
  engine: string;
  database: string;
  compress: boolean;
}

export interface RestoreDatabaseBackupInput {
  engine: string;
  database: string;
  backupName: string;
  dropExisting: boolean;
}

export interface RunQueryInput {
  engine: string;
  database: string;
  query: string;
  mode: "read" | "write";
}

/** Every mutation the Databases pages need, over one database's worth of engine calls. */
export function useDatabaseActions() {
  const queryClient = useQueryClient();

  // Every list, not just the engine that changed: a database created or dropped on one engine
  // also changes what the unfiltered "every engine" list shows.
  const refreshList = (): void => {
    void queryClient.invalidateQueries({ queryKey: databaseKeys.lists });
  };

  const createDatabase = useMutation({
    mutationFn: (input: CreateDatabaseInput) =>
      request("post", "/api/databases/databases", {
        body: {
          engine: input.engine,
          name: input.name,
          owner: input.owner ?? null,
          encoding: input.encoding ?? null,
        },
      }),
    onSuccess: (database) => {
      toast.success(`Database '${database.name}' created`);
      refreshList();
    },
  });

  const dropDatabase = useMutation({
    mutationFn: ({ engine, name, force = false }: { engine: string; name: string; force?: boolean }) =>
      request("delete", "/api/databases/databases/{engine}/{name}", { params: { engine, name }, query: { force } }),
    onSuccess: (result, { engine, name }) => {
      toast.success(result.message);
      refreshList();
      // The dropped database's own detail entry, if anything cached one: it no longer exists,
      // so nothing should be able to read a stale answer for it out of the cache. Only once
      // nothing is still watching it (see useServiceActions.ts's own removeQueries, the same
      // reasoning): removing an entry an active observer still needs makes the query client
      // refetch it immediately, and a dropped database can only answer that with a 404.
      queryClient.removeQueries({ queryKey: databaseKeys.detail(engine, name), type: "inactive" });
    },
    onError: (error, { name }) => {
      reportActionError(`Could not drop '${name}'`, error);
    },
  });

  const createUser = useMutation({
    mutationFn: (input: CreateUserInput) =>
      request("post", "/api/databases/users", {
        body: {
          engine: input.engine,
          username: input.username,
          password: input.password ?? null,
          database: input.database ?? null,
          host: input.host,
        },
      }),
    onSuccess: (_result, input) => {
      void queryClient.invalidateQueries({ queryKey: databaseKeys.users(input.engine) });
    },
  });

  const deleteUser = useMutation({
    mutationFn: ({ engine, username, host }: { engine: string; username: string; host: string }) =>
      request("delete", "/api/databases/users/{engine}/{username}", { params: { engine, username }, query: { host } }),
    onSuccess: (result, { engine }) => {
      toast.success(result.message);
      void queryClient.invalidateQueries({ queryKey: databaseKeys.users(engine) });
    },
    onError: (error, { username }) => {
      reportActionError(`Could not delete '${username}'`, error);
    },
  });

  const grant = useMutation({
    mutationFn: (input: GrantInput) =>
      request("post", "/api/databases/users/grant", {
        body: {
          engine: input.engine,
          username: input.username,
          database: input.database,
          host: input.host,
          privileges: input.privileges.length > 0 ? input.privileges : null,
        },
      }),
    onSuccess: (result, input) => {
      toast.success(result.message);
      void queryClient.invalidateQueries({ queryKey: databaseKeys.users(input.engine) });
    },
  });

  const revoke = useMutation({
    mutationFn: (input: GrantInput) =>
      request("post", "/api/databases/users/revoke", {
        body: {
          engine: input.engine,
          username: input.username,
          database: input.database,
          host: input.host,
          privileges: input.privileges.length > 0 ? input.privileges : null,
        },
      }),
    onSuccess: (result, input) => {
      toast.success(result.message);
      void queryClient.invalidateQueries({ queryKey: databaseKeys.users(input.engine) });
    },
  });

  const createBackup = useMutation({
    mutationFn: (input: CreateDatabaseBackupInput) =>
      request("post", "/api/databases/backups", {
        body: { engine: input.engine, database: input.database, compress: input.compress },
      }),
    onSuccess: (backup) => {
      toast.success(`Backup of '${backup.database}' created`);
      void queryClient.invalidateQueries({ queryKey: databaseKeys.backups(backup.engine, backup.database) });
      void queryClient.invalidateQueries({ queryKey: databaseKeys.backups(null, null) });
    },
  });

  const restoreBackup = useMutation({
    mutationFn: (input: RestoreDatabaseBackupInput) =>
      request("post", "/api/databases/backups/restore", {
        body: {
          engine: input.engine,
          database: input.database,
          backup_name: input.backupName,
          drop_existing: input.dropExisting,
        },
      }),
    onSuccess: (result, input) => {
      toast.success(result.message);
      void queryClient.invalidateQueries({ queryKey: databaseKeys.detail(input.engine, input.database) });
    },
  });

  const runQuery = useMutation({
    mutationFn: (input: RunQueryInput) =>
      request("post", "/api/databases/query", {
        body: {
          engine: input.engine,
          database: input.database,
          query: input.query,
          mode: input.mode,
          max_rows: 500,
        },
      }),
  });

  const buildConnectionString = useMutation({
    mutationFn: (input: { engine: string; database: string; username: string; password: string; host: string }) =>
      request("post", "/api/databases/connection-string", {
        body: {
          engine: input.engine,
          database: input.database,
          username: input.username,
          password: input.password,
          host: input.host,
        },
      }),
  });

  return {
    createDatabase,
    dropDatabase,
    createUser,
    deleteUser,
    grant,
    revoke,
    createBackup,
    restoreBackup,
    runQuery,
    buildConnectionString,
  };
}

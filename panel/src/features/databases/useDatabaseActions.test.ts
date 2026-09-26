import { act, renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../app/App";
import { databaseKeys } from "../../api/queries/databases";
import { fakeBackend, json } from "../../test/fakes";
import { useDatabaseActions } from "./useDatabaseActions";

function wrapper(client = createQueryClient()) {
  return { client, Wrapper: ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children) };
}

describe("useDatabaseActions", () => {
  it("invalidates every engine's list, not just the one that changed, when a database is created", async () => {
    fakeBackend({
      "POST /api/databases/databases": () => json(200, { engine: "postgresql", name: "shop", tables: 0 }),
    });
    const { client, Wrapper } = wrapper();
    client.setQueryData(databaseKeys.list(null), { databases: [], total: 0 });
    client.setQueryData(databaseKeys.list("postgresql"), { databases: [], total: 0 });
    client.setQueryData(databaseKeys.list("mysql"), { databases: [], total: 0 });

    const { result } = renderHook(() => useDatabaseActions(), { wrapper: Wrapper });
    await act(async () => {
      await result.current.createDatabase.mutateAsync({ engine: "postgresql", name: "shop" });
    });

    // The unfiltered "every engine" list and every engine-filtered list are all stale, not
    // only the one for the engine the database was created on.
    expect(client.getQueryState(databaseKeys.list(null))?.isInvalidated).toBe(true);
    expect(client.getQueryState(databaseKeys.list("postgresql"))?.isInvalidated).toBe(true);
    expect(client.getQueryState(databaseKeys.list("mysql"))?.isInvalidated).toBe(true);
  });

  it("invalidates every list and drops the cached detail when a database is dropped", async () => {
    fakeBackend({
      "DELETE /api/databases/databases/postgresql/shop": () => json(200, { success: true, message: "Dropped 'shop'" }),
    });
    const { client, Wrapper } = wrapper();
    client.setQueryData(databaseKeys.list(null), { databases: [{ engine: "postgresql", name: "shop", tables: 0 }], total: 1 });
    client.setQueryData(databaseKeys.detail("postgresql", "shop"), { engine: "postgresql", name: "shop", tables: 0 });

    const { result } = renderHook(() => useDatabaseActions(), { wrapper: Wrapper });
    await act(async () => {
      await result.current.dropDatabase.mutateAsync({ engine: "postgresql", name: "shop" });
    });

    expect(client.getQueryState(databaseKeys.list(null))?.isInvalidated).toBe(true);
    expect(client.getQueryData(databaseKeys.detail("postgresql", "shop"))).toBeUndefined();
  });
});

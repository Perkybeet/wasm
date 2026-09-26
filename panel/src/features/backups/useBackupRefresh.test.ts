import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";

import { backupKeys } from "../../api/queries/backups";
import { ANONYMOUS, FakeEventSource, fakeBackend, json } from "../../test/fakes";
import { useServerEvents } from "../../realtime/events";
import { useBackupRefresh } from "./useBackupRefresh";

function mount(client: QueryClient) {
  fakeBackend({ "GET /api/auth/session": () => json(200, { ...ANONYMOUS, authenticated: true }) });
  return renderHook(
    () => {
      useServerEvents((url) => new FakeEventSource(url));
      useBackupRefresh();
    },
    { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
  );
}

describe("useBackupRefresh", () => {
  it("refreshes the list and the storage figure once a backup job ends", () => {
    const client = new QueryClient();
    client.setQueryData(backupKeys.list(null), { backups: [] });
    client.setQueryData(backupKeys.storage, { used_bytes: 0 });
    mount(client);

    FakeEventSource.latest().emit("job", { id: "j1", type: "backup", status: "running", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(backupKeys.list(null))?.isInvalidated).toBe(false);

    FakeEventSource.latest().emit("job", { id: "j1", type: "backup", status: "completed", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(backupKeys.list(null))?.isInvalidated).toBe(true);
    expect(client.getQueryState(backupKeys.storage)?.isInvalidated).toBe(true);
  });

  it("refreshes on a finished restore job too", () => {
    const client = new QueryClient();
    client.setQueryData(backupKeys.list(null), { backups: [] });
    mount(client);

    FakeEventSource.latest().emit("job", { id: "j2", type: "restore", status: "failed", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(backupKeys.list(null))?.isInvalidated).toBe(true);
  });

  it("ignores jobs of other kinds", () => {
    const client = new QueryClient();
    client.setQueryData(backupKeys.list(null), { backups: [] });
    mount(client);

    FakeEventSource.latest().emit("job", { id: "j3", type: "deploy", status: "completed", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(backupKeys.list(null))?.isInvalidated).toBe(false);
  });
});

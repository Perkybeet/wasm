import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApi } from "../api/client";
import { appKeys } from "../api/queries/apps";
import { jobKeys } from "../api/queries/jobs";
import { systemKeys } from "../api/queries/system";
import { toast } from "../components/ui/toast";
import { ANONYMOUS, FakeEventSource, MACHINE, fakeBackend, json } from "../test/fakes";
import { RECONNECT, reconnectDelay } from "./backoff";
import { EventStream, applyServerEvent } from "./events";

describe("applyServerEvent", () => {
  it("patches the app's own entry and invalidates the list on an app event", () => {
    const client = new QueryClient();
    client.setQueryData(appKeys.list, { apps: [], total: 0 });
    client.setQueryData(appKeys.detail("shop.example.com"), { domain: "shop.example.com", status: "running", port: 3000 });
    client.setQueryData(appKeys.detail("api.example.com"), { domain: "api.example.com", status: "running" });

    applyServerEvent(client, "app", { domain: "shop.example.com", status: "failed" });

    expect(client.getQueryState(appKeys.list)?.isInvalidated).toBe(true);
    expect(client.getQueryData(appKeys.detail("shop.example.com"))).toEqual({
      domain: "shop.example.com",
      status: "failed",
      port: 3000,
    });
    // Other apps are untouched: one app's change does not refetch the fleet.
    expect(client.getQueryState(appKeys.detail("api.example.com"))?.isInvalidated).toBe(false);
  });

  it("writes the machine snapshot where the strip reads it", () => {
    const client = new QueryClient();
    applyServerEvent(client, "machine", MACHINE);
    expect(client.getQueryData(systemKeys.machine)).toEqual(MACHINE);
  });

  it("refetches job lists on a change of status, not on every progress tick", () => {
    const client = new QueryClient();
    client.setQueryData(jobKeys.list({}), { jobs: [] });
    const job = { id: "j1", status: "running", progress: 10, metadata: { domain: "shop.example.com" } };

    applyServerEvent(client, "job", job);
    expect(client.getQueryState(jobKeys.list({}))?.isInvalidated).toBe(true);

    client.setQueryData(jobKeys.list({}), { jobs: [] });
    applyServerEvent(client, "job", { ...job, progress: 40 });
    expect(client.getQueryData(jobKeys.detail("j1"))).toMatchObject({ progress: 40 });
    expect(client.getQueryState(jobKeys.list({}))?.isInvalidated).toBe(false);
  });

  it("refreshes the app a finished job acted on", () => {
    const client = new QueryClient();
    client.setQueryData(appKeys.detail("shop.example.com"), { domain: "shop.example.com" });
    applyServerEvent(client, "job", { id: "j1", status: "completed", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(appKeys.detail("shop.example.com"))?.isInvalidated).toBe(true);
  });

  it("shows a notice in the system's words, as an error when something failed", () => {
    const error = vi.spyOn(toast, "error");
    const success = vi.spyOn(toast, "success");
    applyServerEvent(new QueryClient(), "notice", { text: "npm ERR! missing script: build", state: "failed" });
    applyServerEvent(new QueryClient(), "notice", { text: "Deploy shop.example.com", state: "active" });
    expect(error).toHaveBeenCalledWith("npm ERR! missing script: build");
    expect(success).toHaveBeenCalledWith("Deploy shop.example.com");
  });

  it("ignores payloads that are not the shape it expects", () => {
    const client = new QueryClient();
    applyServerEvent(client, "app", { status: "failed" });
    applyServerEvent(client, "job", ["not", "an", "object"]);
    expect(client.getQueryCache().getAll()).toHaveLength(0);
  });
});

describe("reconnectDelay", () => {
  it("starts at a second, doubles, and stops at thirty", () => {
    expect([0, 1, 2, 3, 4, 5, 6, 10].map(reconnectDelay)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    expect(RECONNECT.maxMs).toBe(30_000);
  });
});

describe("EventStream", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const start = (onDrop = vi.fn()) => {
    const events: [string, unknown][] = [];
    const statuses: string[] = [];
    const stream = new EventStream({
      connect: (url) => new FakeEventSource(url),
      onEvent: (name, data) => events.push([name, data]),
      onStatus: (status) => statuses.push(status),
      onDrop,
    });
    stream.start();
    return { stream, events, statuses, onDrop };
  };

  it("opens one source on /events and dispatches its named JSON events", () => {
    const { events, statuses } = start();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.latest().url).toBe("/events");
    FakeEventSource.latest().open();
    FakeEventSource.latest().emit("machine", MACHINE);
    expect(events).toEqual([["machine", MACHINE]]);
    expect(statuses).toEqual(["connecting", "live"]);
  });

  it("drops a malformed frame and keeps the stream", () => {
    const logged = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { events } = start();
    FakeEventSource.latest().emit("app", "{not json");
    FakeEventSource.latest().emit("app", { domain: "a.example.com" });
    expect(events).toEqual([["app", { domain: "a.example.com" }]]);
    expect(logged).toHaveBeenCalledOnce();
  });

  it("backs off 1s, 2s, 4s... up to 30s, and starts again from 1s after a success", () => {
    const { statuses, onDrop } = start();
    const waits: number[] = [];
    for (let attempt = 0; attempt < 7; attempt += 1) {
      const before = FakeEventSource.instances.length;
      FakeEventSource.latest().fail();
      expect(FakeEventSource.instances[before - 1]?.closed).toBe(true);
      let waited = 0;
      while (FakeEventSource.instances.length === before) {
        vi.advanceTimersByTime(500);
        waited += 500;
      }
      waits.push(waited);
    }
    expect(waits).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    expect(statuses.at(-1)).toBe("reconnecting");
    expect(onDrop).toHaveBeenLastCalledWith(7);

    FakeEventSource.latest().open();
    expect(statuses.at(-1)).toBe("live");
    const before = FakeEventSource.instances.length;
    FakeEventSource.latest().fail();
    vi.advanceTimersByTime(999);
    expect(FakeEventSource.instances).toHaveLength(before);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(before + 1);
  });

  it("stops reconnecting once stopped", () => {
    const { stream } = start();
    FakeEventSource.latest().fail();
    stream.stop();
    vi.advanceTimersByTime(60_000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});

describe("useServerEvents", () => {
  it("sends the operator to sign in when the stream drops because the session ended", async () => {
    const { renderHook } = await import("@testing-library/react");
    const { QueryClientProvider } = await import("@tanstack/react-query");
    const { createElement } = await import("react");
    const { useServerEvents } = await import("./events");

    fakeBackend({ "GET /api/auth/session": () => json(200, ANONYMOUS) });
    const onSessionExpired = vi.fn();
    configureApi({ onSessionExpired });
    const client = new QueryClient();
    renderHook(() => {
      useServerEvents((url) => new FakeEventSource(url));
    }, { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) });

    FakeEventSource.latest().fail();
    await vi.waitFor(() => {
      expect(onSessionExpired).toHaveBeenCalledOnce();
    });
  });
});

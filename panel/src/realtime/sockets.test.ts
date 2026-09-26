import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement } from "react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { configureApi } from "../api/client";
import { jobKeys } from "../api/queries/jobs";
import { FakeWebSocket } from "../test/fakes";
import { StreamSocket, WS_CLOSE_MAX_LIFETIME, WS_CLOSE_UNAUTHORIZED, useJobStream, useLogStream } from "./sockets";

const connect = (url: string) => new FakeWebSocket(url);

async function flush(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe("StreamSocket", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("opens with a fresh single-use ticket and the query it was given", async () => {
    let issued = 0;
    const socket = new StreamSocket({
      path: "/ws/logs/shop.example.com",
      query: () => ({ lines: "200" }),
      getTicket: () => Promise.resolve(`ticket-${String((issued += 1))}`),
      connect,
      onFrame: () => undefined,
      onStatus: () => undefined,
    });
    socket.start();
    await flush();
    const url = new URL(FakeWebSocket.latest().url);
    expect(url.protocol).toBe("ws:");
    expect(url.pathname).toBe("/ws/logs/shop.example.com");
    expect(url.searchParams.get("lines")).toBe("200");
    expect(url.searchParams.get("ticket")).toBe("ticket-1");

    FakeWebSocket.latest().drop();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(new URL(FakeWebSocket.latest().url).searchParams.get("ticket")).toBe("ticket-2");
    socket.stop();
  });

  it("treats a 4401 close as the end of the session", async () => {
    const onSessionExpired = vi.fn();
    configureApi({ onSessionExpired });
    const statuses: string[] = [];
    const socket = new StreamSocket({
      path: "/ws/jobs/j1",
      getTicket: () => Promise.resolve("t"),
      connect,
      onFrame: () => undefined,
      onStatus: (status) => statuses.push(status),
    });
    socket.start();
    await flush();
    FakeWebSocket.latest().drop(WS_CLOSE_UNAUTHORIZED);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(onSessionExpired).toHaveBeenCalledOnce();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(statuses.at(-1)).toBe("closed");
  });

  it("reconnects at once and quietly on a 4408 close, its maximum lifetime reached", async () => {
    let issued = 0;
    const statuses: string[] = [];
    const socket = new StreamSocket({
      path: "/ws/jobs/j1",
      getTicket: () => Promise.resolve(`ticket-${String((issued += 1))}`),
      connect,
      onFrame: () => undefined,
      onStatus: (status) => statuses.push(status),
    });
    socket.start();
    await flush();
    FakeWebSocket.latest().open();
    expect(statuses).toEqual(["connecting", "open"]);

    FakeWebSocket.latest().drop(WS_CLOSE_MAX_LIFETIME);
    await flush();
    FakeWebSocket.latest().open();
    // A new connection, immediately: "connecting" again (the same neutral state the first
    // connection went through), never "reconnecting" - the pill a real drop shows.
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(new URL(FakeWebSocket.latest().url).searchParams.get("ticket")).toBe("ticket-2");
    expect(statuses).toEqual(["connecting", "open", "connecting", "open"]);
    expect(statuses).not.toContain("reconnecting");
    socket.stop();
  });
});

describe("useLogStream", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("collects journal lines, marks warnings, and reports the stream's own error", async () => {
    const { result } = renderHook(() => useLogStream("shop.example.com", { getTicket: () => Promise.resolve("t"), connect }));
    await flush();
    const socket = FakeWebSocket.latest();
    act(() => {
      socket.open();
      socket.frame({ type: "connected", domain: "shop.example.com", service: "wasm-shop" });
      socket.frame({ type: "log", data: "2026-09-25T10:00:00 shop[1]: listening on :3000" });
      socket.frame({ type: "warning", data: "journalctl: some lines were rotated" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60);
    });
    expect(result.current.status).toBe("open");
    expect(result.current.lines.map((line) => [line.text, line.level])).toEqual([
      ["2026-09-25T10:00:00 shop[1]: listening on :3000", undefined],
      ["journalctl: some lines were rotated", "warn"],
    ]);

    act(() => {
      socket.frame({ type: "error", message: "journalctl not found. Log streaming requires systemd." });
    });
    expect(result.current.error).toBe("journalctl not found. Log streaming requires systemd.");
  });

  it("keeps the newest lines under the cap and says it dropped some", async () => {
    const { result } = renderHook(() =>
      useLogStream("shop.example.com", { cap: 3, getTicket: () => Promise.resolve("t"), connect }),
    );
    await flush();
    act(() => {
      for (let n = 1; n <= 5; n += 1) FakeWebSocket.latest().frame({ type: "log", data: `line ${String(n)}` });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60);
    });
    expect(result.current.lines.map((line) => line.text)).toEqual(["line 3", "line 4", "line 5"]);
    expect(result.current.truncated).toBe(true);
  });

  it("asks for one line of backlog after a reconnection and drops the echo", async () => {
    const { result } = renderHook(() => useLogStream("shop.example.com", { getTicket: () => Promise.resolve("t"), connect }));
    await flush();
    act(() => {
      FakeWebSocket.latest().frame({ type: "log", data: "a" });
      FakeWebSocket.latest().frame({ type: "log", data: "b" });
      FakeWebSocket.latest().drop();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(new URL(FakeWebSocket.latest().url).searchParams.get("lines")).toBe("1");
    act(() => {
      FakeWebSocket.latest().frame({ type: "log", data: "b" });
      FakeWebSocket.latest().frame({ type: "log", data: "c" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60);
    });
    expect(result.current.lines.map((line) => line.text)).toEqual(["a", "b", "c"]);
  });
});

describe("useJobStream", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("follows a job until it finishes, keeping the job's query entry in step", async () => {
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
    const { result } = renderHook(() => useJobStream("j1", { getTicket: () => Promise.resolve("t"), connect }), { wrapper });
    await flush();
    const job = { id: "j1", status: "running", progress: 10 };
    act(() => {
      FakeWebSocket.latest().frame({ type: "connected", job });
      FakeWebSocket.latest().frame({ type: "heartbeat" });
      FakeWebSocket.latest().frame({ type: "update", job: { ...job, progress: 60 } });
    });
    expect(result.current.job).toMatchObject({ progress: 60 });
    expect(client.getQueryData(jobKeys.detail("j1"))).toMatchObject({ progress: 60 });

    act(() => {
      FakeWebSocket.latest().frame({ type: "finished", job: { ...job, status: "completed", progress: 100 } });
      FakeWebSocket.latest().drop(1000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(result.current.finished).toBe(true);
    expect(result.current.status).toBe("closed");
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});

/**
 * Stand-ins for the network, for tests: the API behind `fetch`, the `/events` stream and the
 * WebSockets. They speak the backend's wire format (the error contract, the session answer,
 * the machine snapshot) so the console under test runs its real code paths.
 */

import { vi } from "vitest";

import type { SessionInfo } from "../api/queries/auth";
import type { Machine } from "../api/queries/system";
import type { EventSourceLike } from "../realtime/events";
import type { SocketLike } from "../realtime/sockets";

export const SESSION: SessionInfo = {
  authenticated: true,
  scope: "admin",
  expires_at: "2026-09-25T20:00:00+00:00",
  elevated_until: null,
  totp_enabled: true,
  hostname: "web-01",
  version: "2.0.0",
  csrf_header: "X-WASM-CSRF",
  csrf_cookie: "wasm_csrf",
};

export const ANONYMOUS: SessionInfo = { ...SESSION, authenticated: false, scope: null, expires_at: null };

export const MACHINE: Machine = {
  hostname: "web-01",
  uptime_s: 12 * 86_400 + 4 * 3_600,
  load: [0.42, 0.38, 0.31],
  load_history: [0.3, 0.4, 0.5, 0.42],
  cpu_percent: 18.5,
  memory: { used: 3_380_000_000, total: 8_000_000_000, percent: 42.3 },
  disk: { used: 61_000_000_000, total: 78_000_000_000, percent: 78.2 },
  units: { running: 9, failed: 1, stopped: 2 },
  apps: { running: 4, failed: 1, stopped: 1, static: 1 },
};

export function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/** A failure in the API's contract: {error, detail, hint, fields}. */
export function problem(
  status: number,
  error: string,
  detail: string,
  extra: { hint?: string; fields?: Record<string, string>; headers?: Record<string, string> } = {},
): Response {
  return json(status, { error, detail, hint: extra.hint ?? null, fields: extra.fields ?? null }, extra.headers);
}

export interface RecordedCall {
  method: string;
  path: string;
  search: URLSearchParams;
  headers: Headers;
  body: unknown;
}

export type RouteHandler = (call: RecordedCall) => Response | Promise<Response>;

export interface FakeBackend {
  calls: RecordedCall[];
  /** Replaces or adds a route: "GET /api/apps". */
  on: (route: string, handler: RouteHandler) => void;
  callsTo: (route: string) => RecordedCall[];
}

/**
 * Installs a fake API behind `fetch`. Routes are "METHOD /path" with exact paths; anything
 * unrouted answers 404 in the contract's shape and is still recorded.
 */
export function fakeBackend(routes: Record<string, RouteHandler> = {}): FakeBackend {
  const table = new Map(Object.entries(routes));
  const calls: RecordedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const href = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const url = new URL(href, "http://console.test");
      const text = typeof init.body === "string" ? init.body : undefined;
      const call: RecordedCall = {
        method: (init.method ?? "GET").toUpperCase(),
        path: url.pathname,
        search: url.searchParams,
        headers: new Headers(init.headers),
        body: text === undefined ? undefined : (JSON.parse(text) as unknown),
      };
      calls.push(call);
      const handler = table.get(`${call.method} ${call.path}`);
      if (!handler) return problem(404, "not_found", `No fake for ${call.method} ${call.path}`);
      return handler(call);
    }),
  );
  return {
    calls,
    on: (route, handler) => {
      table.set(route, handler);
    },
    callsTo: (route) => calls.filter((call) => `${call.method} ${call.path}` === route),
  };
}

/** The routes every signed-in page needs. */
export function signedInRoutes(session: SessionInfo = SESSION): Record<string, RouteHandler> {
  return {
    "GET /api/auth/session": () => json(200, session),
    "GET /api/system/machine": () => json(200, MACHINE),
    "GET /api/apps": () =>
      json(200, {
        total: 2,
        apps: [
          { domain: "shop.example.com", name: "shop", app_type: "nextjs", status: "running", active: true, enabled: true },
          { domain: "admin.example.com", name: "admin", app_type: "python", status: "failed", active: false, enabled: true },
        ],
      }),
  };
}

export class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closed = false;
  readonly url: string;
  private readonly listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  static latest(): FakeEventSource {
    const source = FakeEventSource.instances.at(-1);
    if (!source) throw new Error("No EventSource was opened");
    return source;
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  open(): void {
    this.onopen?.(new Event("open"));
  }

  fail(): void {
    this.onerror?.(new Event("error"));
  }

  emit(type: string, data: unknown): void {
    const event = new MessageEvent<string>(type, { data: typeof data === "string" ? data : JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

export class FakeWebSocket implements SocketLike {
  static instances: FakeWebSocket[] = [];
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  closedWith: number | null = null;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  static latest(): FakeWebSocket {
    const socket = FakeWebSocket.instances.at(-1);
    if (!socket) throw new Error("No WebSocket was opened");
    return socket;
  }

  close(code = 1000): void {
    this.closedWith = code;
  }

  open(): void {
    this.onopen?.(new Event("open"));
  }

  frame(data: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }

  /** The server closed the connection. */
  drop(code = 1006): void {
    this.onclose?.(new CloseEvent("close", { code }));
  }
}

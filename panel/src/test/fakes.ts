/**
 * Stand-ins for the network, for tests: the API behind `fetch`, the `/events` stream and the
 * WebSockets. They speak the backend's wire format (the error contract, the session answer,
 * the machine snapshot) so the console under test runs its real code paths.
 */

import { vi } from "vitest";

import type { SessionInfo } from "../api/queries/auth";
import type { CronJobList } from "../api/queries/cron";
import type { JobList } from "../api/queries/jobs";
import type { MonitorSettings, MonitorStatus, ObservationList } from "../api/queries/monitor";
import type { ServiceList } from "../api/queries/services";
import type { Machine, NetworkInfo, ProcessList, SystemHealth, SystemInfo } from "../api/queries/system";
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

/** The applications of the fake machine, as GET /api/apps lists them. */
export const APPS = [
  { domain: "shop.example.com", name: "shop", app_type: "nextjs", status: "running", active: true, enabled: true, port: 3000, layout: "inplace" },
  { domain: "admin.example.com", name: "admin", app_type: "python", status: "failed", active: false, enabled: true, port: 8000, layout: "inplace" },
] as const;

/** The services of the fake machine, as GET /api/services lists them. */
export const SERVICES: ServiceList["services"] = [
  { name: "wasm-shop", description: "node /var/www/shop/server.js", active: true, enabled: true, status: "running", pid: 4821, uptime: "Thu 2026-09-25 08:00:00 UTC", memory: "58720256" },
];

/** The cron jobs of the fake machine, as GET /api/cron lists them. */
export const CRON_JOBS: CronJobList["jobs"] = [
  {
    name: "nightly-backup",
    command: "wasm backup create shop.example.com",
    user: "wasm",
    working_directory: "/var/www/shop",
    app_domain: "shop.example.com",
    schedule: "daily",
    on_calendar: "*-*-* 02:00:00",
    enabled: true,
    next_run: "Fri 2026-09-26 02:00:00 UTC",
    last_run: "Thu 2026-09-25 02:00:00 UTC",
    last_exit_code: 0,
    last_result: "success",
  },
];

/** The jobs of the fake machine, as GET /api/jobs lists them. */
export const JOBS: JobList["jobs"] = [
  {
    id: "a1b2c3d4",
    type: "update",
    name: "Update shop.example.com",
    description: "Updating the application at shop.example.com",
    status: "completed",
    progress: 100,
    total_steps: 100,
    current_step: "",
    created_at: "2026-09-25T09:00:00Z",
    started_at: "2026-09-25T09:00:01Z",
    completed_at: "2026-09-25T09:00:42Z",
    result: null,
    error: null,
    logs: [],
    metadata: { domain: "shop.example.com" },
  },
];

export const MONITOR_STATUS: MonitorStatus = { installed: true, enabled: true, active: true, pid: 512, uptime: "Thu 2026-09-25 08:00:00 UTC", scope: ["reads /proc, never signals a process"] };

export const MONITOR_CONFIG: MonitorSettings = {
  scan_interval: 60,
  cpu_threshold: 80,
  memory_threshold: 80,
  retention_days: 30,
  max_observations: 500,
  notify: false,
  watch_units: [],
};

export const OBSERVATIONS: ObservationList["observations"] = [];

export const SYSTEM_HEALTH: SystemHealth = {
  verdict: "healthy",
  checks: [
    { name: "Disk Space", value: "61 GB of 78 GB used", status: "ok" },
    { name: "Nginx", value: "active", status: "ok" },
  ],
  issues: [],
  warnings: [],
};

export const SYSTEM_INFO: SystemInfo = {
  hostname: "web-01",
  os: "Ubuntu 24.04 LTS",
  kernel: "6.8.0-generic",
  uptime: "12d 4h 0m",
  cpu: { cores: 4, percent: 18.5, load_1min: 0.42, load_5min: 0.38, load_15min: 0.31 },
  memory: { total_gb: 8, used_gb: 3.38, free_gb: 4.62, available_gb: 4.9, percent_used: 42.3, swap_total_gb: 2, swap_used_gb: 0, swap_percent: 0 },
  disks: [{ device: "/dev/sda1", mount_point: "/", total_gb: 78, used_gb: 61, free_gb: 17, percent_used: 78.2 }],
};

export const NETWORK: NetworkInfo = {
  interfaces: [{ name: "eth0", addresses: [{ type: "IPv4", address: "10.0.0.5", netmask: "255.255.255.0" }], is_up: true, speed_mbps: 1000, bytes_sent: 128_000_000, bytes_recv: 512_000_000, packets_sent: 90_000, packets_recv: 210_000 }],
};

export const PROCESSES: ProcessList = {
  total: 1,
  processes: [{ pid: 4821, name: "node", cpu_percent: 2.1, memory_percent: 3.4, memory_mb: 210.5, status: "running", user: "wasm", command: "node server.js" }],
};

/** The routes every signed-in page needs. */
export function signedInRoutes(session: SessionInfo = SESSION): Record<string, RouteHandler> {
  return {
    "GET /api/auth/session": () => json(200, session),
    "GET /api/system/machine": () => json(200, MACHINE),
    "GET /api/apps": () => json(200, { total: APPS.length, apps: APPS }),
    // Each app's own page reads it by domain.
    ...Object.fromEntries(APPS.map((app) => [`GET /api/apps/${app.domain}`, () => json(200, app)])),
    "GET /api/services": () => json(200, { services: SERVICES, total: SERVICES.length }),
    ...Object.fromEntries(SERVICES.map((service) => [`GET /api/services/${service.name}`, () => json(200, service)])),
    "GET /api/cron": () => json(200, { jobs: CRON_JOBS, total: CRON_JOBS.length }),
    "GET /api/jobs": () => json(200, { jobs: JOBS, total: JOBS.length, active: 0 }),
    "GET /api/monitor/status": () => json(200, MONITOR_STATUS),
    "GET /api/monitor/config": () => json(200, MONITOR_CONFIG),
    "GET /api/monitor/observations": () => json(200, { observations: OBSERVATIONS, count: OBSERVATIONS.length, stats: {} }),
    "GET /api/system/health": () => json(200, SYSTEM_HEALTH),
    "GET /api/system": () => json(200, SYSTEM_INFO),
    "GET /api/system/network": () => json(200, NETWORK),
    "GET /api/system/processes": () => json(200, PROCESSES),
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

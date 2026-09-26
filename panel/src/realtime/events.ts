/**
 * The one server-sent event stream, `/events`.
 *
 * Every tab holds exactly one EventSource (a browser caps connections per origin, and the
 * backend multiplexes every concern onto this stream). Events land in two places: the
 * TanStack Query cache, so any page showing the data updates without polling, and the
 * subscribers of `useServerEvent`, for pages that react to a moment (a deploy finishing)
 * rather than to data.
 *
 * The browser's own EventSource retry runs at a fixed interval forever; this closes the
 * source on error and reconnects on an exponential backoff instead, and checks whether the
 * session is still alive, because an EventSource cannot see the 401 that ended it.
 */

import { useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useSyncExternalStore } from "react";

import { expireSession } from "../api/client";
import { appKeys } from "../api/queries/apps";
import type { App } from "../api/queries/apps";
import { sessionQuery } from "../api/queries/auth";
import { deploymentKeys } from "../api/queries/deployments";
import { jobKeys } from "../api/queries/jobs";
import type { Job } from "../api/queries/jobs";
import { metricKeys } from "../api/queries/metrics";
import type { MetricsSnapshot } from "../api/queries/metrics";
import { systemKeys } from "../api/queries/system";
import type { Machine } from "../api/queries/system";
import { toast } from "../components/ui/toast";
import { reconnectDelay } from "./backoff";

/** A state change of one application. Carries its domain and the fields that changed. */
export type AppEvent = { domain: string } & Partial<Exclude<App, undefined>>;

/** A job snapshot, the same shape GET /api/jobs/{id} answers. */
export type JobEvent = Job;

/** Something the operator should hear about, in the system's words. */
export interface NoticeEvent {
  text: string;
  /** The backend's four-word vocabulary: busy, active, failed, idle. */
  state: "busy" | "active" | "failed" | "idle";
}

/** The pre-v2 transition event: an id (a job id or a domain) moved to a state. */
export interface StateEvent {
  id: string;
  state: NoticeEvent["state"];
}

export interface ServerEventMap {
  machine: Machine;
  metrics: MetricsSnapshot;
  app: AppEvent;
  job: JobEvent;
  notice: NoticeEvent;
  state: StateEvent;
}

export type ServerEventName = keyof ServerEventMap;

export const SERVER_EVENTS: readonly ServerEventName[] = ["machine", "metrics", "app", "job", "notice", "state"];

export type StreamStatus = "connecting" | "live" | "reconnecting";

/** The part of EventSource the stream uses, so tests can drive it. */
export interface EventSourceLike {
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

export interface EventStreamOptions {
  url?: string;
  connect?: (url: string) => EventSourceLike;
  onEvent: (name: ServerEventName, data: unknown) => void;
  onStatus?: (status: StreamStatus) => void;
  /** Called after every dropped connection with the count of consecutive failures. */
  onDrop?: (failures: number) => void;
}

/** One EventSource with exponential reconnection. Framework-free; the hook below drives it. */
export class EventStream {
  private readonly url: string;
  private readonly connect: (url: string) => EventSourceLike;
  private readonly options: EventStreamOptions;
  private source: EventSourceLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private running = false;

  constructor(options: EventStreamOptions) {
    this.options = options;
    this.url = options.url ?? "/events";
    this.connect = options.connect ?? ((url) => new EventSource(url, { withCredentials: true }));
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.failures = 0;
    this.open();
  }

  stop(): void {
    this.running = false;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.source?.close();
    this.source = null;
  }

  private open(): void {
    this.timer = null;
    this.options.onStatus?.(this.failures === 0 ? "connecting" : "reconnecting");
    const source = this.connect(this.url);
    this.source = source;

    source.onopen = () => {
      this.failures = 0;
      this.options.onStatus?.("live");
    };
    source.onerror = () => {
      source.close();
      if (!this.running || this.source !== source) return;
      this.source = null;
      const delay = reconnectDelay(this.failures);
      this.failures += 1;
      this.options.onStatus?.("reconnecting");
      this.options.onDrop?.(this.failures);
      this.timer = setTimeout(() => {
        this.open();
      }, delay);
    };
    for (const name of SERVER_EVENTS) {
      source.addEventListener(name, (event) => {
        let data: unknown;
        try {
          data = JSON.parse(event.data);
        } catch {
          // A frame that is not JSON is a backend bug; drop it loudly, keep the stream.
          console.error(`Ignored a malformed "${name}" event from /events:`, event.data);
          return;
        }
        this.options.onEvent(name, data);
      });
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const TERMINAL_JOB_STATES = new Set(["completed", "failed", "cancelled"]);

/**
 * Writes one server event into the query cache. Pure over the client passed in, so every
 * cache effect of the stream is tested without a stream.
 */
export function applyServerEvent(queryClient: QueryClient, name: ServerEventName, data: unknown): void {
  if (!isRecord(data)) return;
  switch (name) {
    case "machine":
      queryClient.setQueryData<Machine>(systemKeys.machine, data as unknown as Machine);
      return;
    case "metrics":
      queryClient.setQueryData<MetricsSnapshot>(metricKeys.latest, data as MetricsSnapshot);
      return;
    case "app": {
      const { domain, ...changes } = data;
      if (typeof domain !== "string") return;
      void queryClient.invalidateQueries({ queryKey: appKeys.list, exact: true });
      queryClient.setQueryData(appKeys.detail(domain), (current: unknown) =>
        isRecord(current) ? { ...current, ...changes } : current,
      );
      return;
    }
    case "job": {
      const job = data as unknown as Job;
      if (typeof job.id !== "string") return;
      const previous = queryClient.getQueryData<Job>(jobKeys.detail(job.id));
      queryClient.setQueryData(jobKeys.detail(job.id), job);
      // Progress and log lines arrive as job events too; only a change of status changes a
      // list, so only that refetches one.
      if (previous?.status === job.status) return;
      void queryClient.invalidateQueries({ queryKey: jobKeys.all });
      void queryClient.invalidateQueries({ queryKey: deploymentKeys.all });
      const domain = job.metadata?.["domain"];
      if (typeof domain === "string" && TERMINAL_JOB_STATES.has(job.status)) {
        void queryClient.invalidateQueries({ queryKey: appKeys.detail(domain) });
      }
      return;
    }
    case "notice": {
      const text = typeof data["text"] === "string" ? data["text"] : null;
      if (text === null) return;
      if (data["state"] === "failed") toast.error(text);
      else if (data["state"] === "active") toast.success(text);
      else toast.info(text);
      return;
    }
    case "state": {
      if (typeof data["id"] !== "string") return;
      void queryClient.invalidateQueries({ queryKey: appKeys.list, exact: true });
      void queryClient.invalidateQueries({ queryKey: appKeys.detail(data["id"]) });
      void queryClient.invalidateQueries({ queryKey: jobKeys.all });
      return;
    }
  }
}

// ---------------------------------------------------------------------------------------
// React bindings.

type Handler<N extends ServerEventName> = (data: ServerEventMap[N]) => void;

const subscribers = new Map<ServerEventName, Set<Handler<ServerEventName>>>();
let streamStatus: StreamStatus = "connecting";
const statusListeners = new Set<() => void>();

function setStreamStatus(next: StreamStatus): void {
  if (next === streamStatus) return;
  streamStatus = next;
  for (const listener of statusListeners) listener();
}

function publish(name: ServerEventName, data: unknown): void {
  for (const handler of subscribers.get(name) ?? []) handler(data as ServerEventMap[ServerEventName]);
}

/**
 * Opens the stream for as long as the calling component is mounted. Mount it once, in the
 * authenticated shell: /events needs a session.
 */
export function useServerEvents(connect?: (url: string) => EventSourceLike): void {
  const queryClient = useQueryClient();
  const connectRef = useRef(connect);

  useEffect(() => {
    // Whatever happened while the stream was down (a job ending, an app failing) was said on
    // a connection nobody held: once it is back, everything on screen is read again.
    let dropped = false;
    const stream = new EventStream({
      ...(connectRef.current ? { connect: connectRef.current } : {}),
      onEvent: (name, data) => {
        applyServerEvent(queryClient, name, data);
        publish(name, data);
      },
      onStatus: (status) => {
        setStreamStatus(status);
        if (status === "reconnecting") dropped = true;
        else if (status === "live" && dropped) {
          dropped = false;
          void queryClient.invalidateQueries();
        }
      },
      onDrop: () => {
        // The stream cannot tell a restarting panel from an expired session. The session
        // endpoint can: it answers 200 either way, saying which.
        queryClient
          .query({ ...sessionQuery(), staleTime: 0 })
          .then((session) => {
            if (!session.authenticated) expireSession();
          })
          // Unreachable server: the strip already says "Reconnecting", and the stream retries.
          .catch(() => undefined);
      },
    });
    stream.start();
    return () => {
      stream.stop();
      setStreamStatus("connecting");
    };
  }, [queryClient]);
}

/** Runs `handler` for every event of one name while the component is mounted. */
export function useServerEvent<N extends ServerEventName>(name: N, handler: Handler<N>): void {
  const ref = useRef(handler);
  useEffect(() => {
    ref.current = handler;
  });
  useEffect(() => {
    const relay: Handler<ServerEventName> = (data) => {
      ref.current(data as ServerEventMap[N]);
    };
    let set = subscribers.get(name);
    if (!set) {
      set = new Set();
      subscribers.set(name, set);
    }
    set.add(relay);
    return () => {
      set.delete(relay);
    };
  }, [name]);
}

/** Whether the live stream is up, for the strip's reconnecting indicator. */
export function useStreamStatus(): StreamStatus {
  return useSyncExternalStore(
    (listener) => {
      statusListeners.add(listener);
      return () => statusListeners.delete(listener);
    },
    () => streamStatus,
  );
}

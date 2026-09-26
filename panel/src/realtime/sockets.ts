/**
 * The two WebSocket streams: an app's journal (`/ws/logs/{domain}`) and one job's progress
 * (`/ws/jobs/{id}`).
 *
 * Every connection is opened with a fresh single-use ticket from POST /api/auth/ws-ticket,
 * because a browser cannot set headers on a handshake and a long-lived token in a query
 * string ends up in proxy logs. A dropped connection reconnects on the same backoff as the
 * event stream, with a new ticket each time; a close with 4401 means the session is gone.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { buildPath, expireSession, isApiError } from "../api/client";
import { wsTicket } from "../api/queries/auth";
import { jobKeys } from "../api/queries/jobs";
import type { Job } from "../api/queries/jobs";
import type { LogLine } from "../components/ui/LogViewer";
import { reconnectDelay } from "./backoff";

export type SocketStatus = "connecting" | "open" | "reconnecting" | "closed";

/** Close code the backend uses for a handshake without a valid session or ticket. */
export const WS_CLOSE_UNAUTHORIZED = 4401;

/** Close code the backend uses when a connection reaches its maximum lifetime: routine
 * housekeeping, not a dropped connection, so it reconnects at once and never tells the
 * operator anything happened. */
export const WS_CLOSE_MAX_LIFETIME = 4408;

/** The part of WebSocket the streams use, so tests can drive it. */
export interface SocketLike {
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  close(code?: number): void;
}

export interface StreamSocketOptions {
  /** Path on this origin, such as `/ws/logs/example.com` (already encoded). */
  path: string;
  /** Extra query parameters; the ticket is added to them. Read at every (re)connection. */
  query?: () => Record<string, string>;
  onFrame: (frame: Record<string, unknown>) => void;
  onStatus: (status: SocketStatus) => void;
  /** True once the stream has nothing more to say (a finished job): no reconnection. */
  isComplete?: () => boolean;
  getTicket?: () => Promise<string>;
  connect?: (url: string) => SocketLike;
}

function socketUrl(path: string, query: Record<string, string>): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}${buildPath(path, undefined, query)}`;
}

/** One reconnecting WebSocket. Framework-free; the hooks below drive it. */
export class StreamSocket {
  private readonly options: StreamSocketOptions;
  private socket: SocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private running = false;

  constructor(options: StreamSocketOptions) {
    this.options = options;
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.failures = 0;
    void this.open();
  }

  stop(): void {
    this.running = false;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    const socket = this.socket;
    this.socket = null;
    socket?.close(1000);
  }

  private retry(): void {
    if (!this.running) return;
    if (this.options.isComplete?.()) {
      this.running = false;
      this.options.onStatus("closed");
      return;
    }
    const delay = reconnectDelay(this.failures);
    this.failures += 1;
    this.options.onStatus("reconnecting");
    this.timer = setTimeout(() => void this.open(), delay);
  }

  private async open(): Promise<void> {
    this.timer = null;
    if (this.failures === 0) this.options.onStatus("connecting");
    let ticket: string;
    try {
      ticket = await (this.options.getTicket ?? wsTicket)();
    } catch (error: unknown) {
      // The client already sent the operator to sign in if the session is gone.
      if (isApiError(error) && error.sessionExpired) {
        this.stop();
        return;
      }
      this.retry();
      return;
    }
    if (!this.running) return;

    const url = socketUrl(this.options.path, { ...(this.options.query?.() ?? {}), ticket });
    const socket = (this.options.connect ?? ((u) => new WebSocket(u)))(url);
    this.socket = socket;

    socket.onopen = () => {
      this.failures = 0;
      this.options.onStatus("open");
    };
    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      let frame: unknown;
      try {
        frame = JSON.parse(event.data);
      } catch {
        console.error(`Ignored a malformed frame from ${this.options.path}:`, event.data);
        return;
      }
      if (typeof frame === "object" && frame !== null) this.options.onFrame(frame as Record<string, unknown>);
    };
    socket.onclose = (event) => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (event.code === WS_CLOSE_UNAUTHORIZED) {
        this.running = false;
        this.options.onStatus("closed");
        expireSession();
        return;
      }
      if (event.code === WS_CLOSE_MAX_LIFETIME) {
        // Expected: reconnect right away, without the backoff a real drop would get and
        // without the "reconnecting" status a real drop would show.
        void this.open();
        return;
      }
      this.retry();
    };
  }
}

// ---------------------------------------------------------------------------------------

export interface LogStream {
  lines: LogLine[];
  status: SocketStatus;
  /** The stream's own error, verbatim (journalctl missing, an invalid domain). */
  error: string | null;
  /** Lines were dropped from the start to stay under the cap. */
  truncated: boolean;
}

export interface LogStreamOptions {
  /** Backlog of journal lines to send first. The backend accepts 1-500. */
  lines?: number;
  /** Most lines held in memory; the oldest go first. */
  cap?: number;
  getTicket?: () => Promise<string>;
  connect?: (url: string) => SocketLike;
}

const FLUSH_MS = 50;

const INITIAL_LOG_STREAM: LogStream = { lines: [], status: "connecting", error: null, truncated: false };
const INITIAL_JOB_STREAM: JobStream = { job: null, status: "connecting", finished: false, error: null };

/**
 * Follows an app's journal. Lines are buffered and committed to state at most every 50ms, so
 * a burst of output costs a handful of renders instead of one per line.
 */
export function useLogStream(domain: string | null, options: LogStreamOptions = {}): LogStream {
  const { lines: backlog = 200, cap = 10_000 } = options;
  const [state, setState] = useState<LogStream>(INITIAL_LOG_STREAM);
  // A different domain starts from nothing, reset while rendering rather than after it.
  const [following, setFollowing] = useState(domain);
  if (following !== domain) {
    setFollowing(domain);
    setState(INITIAL_LOG_STREAM);
  }
  const injected = useRef({ getTicket: options.getTicket, connect: options.connect });

  useEffect(() => {
    if (domain === null) return;
    let nextId = 0;
    let buffer: LogLine[] = [];
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    let connections = 0;
    let lastText: string | null = null;
    let skipEcho = false;

    const flush = (): void => {
      flushTimer = null;
      const incoming = buffer;
      buffer = [];
      setState((current) => {
        const merged = current.lines.concat(incoming);
        const overflow = merged.length - cap;
        return overflow > 0
          ? { ...current, lines: merged.slice(overflow), truncated: true }
          : { ...current, lines: merged };
      });
    };
    const push = (line: Omit<LogLine, "id">): void => {
      buffer.push({ id: nextId++, ...line });
      lastText = line.text;
      flushTimer ??= setTimeout(flush, FLUSH_MS);
    };

    const socket = new StreamSocket({
      path: `/ws/logs/${encodeURIComponent(domain)}`,
      // After a reconnection, ask for one line of backlog and drop it if it is the last line
      // already shown: the full backlog again would duplicate everything on screen.
      query: () => {
        connections += 1;
        skipEcho = connections > 1;
        return { lines: String(connections > 1 ? 1 : backlog) };
      },
      onStatus: (status) => {
        setState((current) => ({ ...current, status }));
      },
      onFrame: (frame) => {
        const text = typeof frame["data"] === "string" ? frame["data"] : null;
        switch (frame["type"]) {
          case "log":
            if (text === null) return;
            if (skipEcho) {
              skipEcho = false;
              if (text === lastText) return;
            }
            push({ text });
            return;
          case "warning":
            if (text !== null) push({ text, level: "warn" });
            return;
          case "error":
            setState((current) => ({
              ...current,
              error: typeof frame["message"] === "string" ? frame["message"] : "The log stream failed.",
            }));
            return;
          default:
            return;
        }
      },
      ...(injected.current.getTicket ? { getTicket: injected.current.getTicket } : {}),
      ...(injected.current.connect ? { connect: injected.current.connect } : {}),
    });
    socket.start();
    return () => {
      socket.stop();
      if (flushTimer !== null) clearTimeout(flushTimer);
    };
  }, [domain, backlog, cap]);

  return state;
}

export interface JobStream {
  job: Job | null;
  status: SocketStatus;
  /** The job reached completed, failed or cancelled. */
  finished: boolean;
  error: string | null;
}

/**
 * Follows one job until it finishes. Every snapshot is also written to the job's query
 * entry, so anything else showing that job stays in step.
 */
export function useJobStream(
  id: string | null,
  options: Pick<LogStreamOptions, "getTicket" | "connect"> = {},
): JobStream {
  const queryClient = useQueryClient();
  const [state, setState] = useState<JobStream>(INITIAL_JOB_STREAM);
  const [following, setFollowing] = useState(id);
  if (following !== id) {
    setFollowing(id);
    setState(INITIAL_JOB_STREAM);
  }
  const injected = useRef(options);

  useEffect(() => {
    if (id === null) return;
    let finished = false;

    const socket = new StreamSocket({
      path: `/ws/jobs/${encodeURIComponent(id)}`,
      isComplete: () => finished,
      onStatus: (status) => {
        setState((current) => ({ ...current, status }));
      },
      onFrame: (frame) => {
        const type = frame["type"];
        if (type === "error") {
          setState((current) => ({
            ...current,
            error: typeof frame["message"] === "string" ? frame["message"] : "The job stream failed.",
          }));
          return;
        }
        if (type !== "connected" && type !== "update" && type !== "finished") return;
        const job = frame["job"] as Job | undefined;
        if (!job || typeof job !== "object") return;
        queryClient.setQueryData(jobKeys.detail(id), job);
        if (type === "finished") finished = true;
        setState((current) => ({ ...current, job, finished: current.finished || type === "finished" }));
      },
      ...(injected.current.getTicket ? { getTicket: injected.current.getTicket } : {}),
      ...(injected.current.connect ? { connect: injected.current.connect } : {}),
    });
    socket.start();
    return () => {
      socket.stop();
    };
  }, [id, queryClient]);

  return state;
}

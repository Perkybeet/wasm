/**
 * A stand-in for the FastAPI backend, for reviewing the console without a server.
 *
 *   npm run dev:mock        (VITE_MOCK=1 vite --port 5199 --strictPort)
 *
 * It lives in the Vite dev server (Node), never in the browser bundle: `apply: "serve"` keeps
 * it out of `vite build`, and nothing under src/ imports it. It answers the same wire format
 * as the real API (the error contract, the session cookies, the CSRF header, 403
 * elevation_required, the lockout) for the handful of endpoints the shell and the sign-in
 * screens use. Everything else answers 404 in the contract's shape, so a page that needs a
 * real endpoint says so instead of pretending.
 *
 * Credentials: access token `wasm_mock_token`, two-factor code `123456` (set MOCK_TOTP=0 to
 * turn two-factor off). POST /api/__mock/expire ends every session, to watch the console
 * handle an expiry.
 */

import { randomBytes } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";

import type { Plugin } from "vite";

const TOKEN = "wasm_mock_token";
const CODE = "123456";
const HOSTNAME = "web-01";
const VERSION = "2.0.0.dev0";
const MAX_FAILURES = 5;
const LOCKOUT_SECONDS = 300;
const SESSION_SECONDS = 8 * 3600;

interface Session {
  csrf: string;
  expiresAt: number;
  elevatedUntil: number | null;
}

interface Snapshot {
  domain: string;
  name: string;
  app_type: string;
  status: "running" | "stopped" | "static" | "failed" | "deploying";
  port: number | null;
}

const APPS: Snapshot[] = [
  { domain: "shop.example.com", name: "shop", app_type: "nextjs", status: "running", port: 3000 },
  { domain: "api.example.com", name: "api", app_type: "nodejs", status: "running", port: 3001 },
  { domain: "admin.example.com", name: "admin", app_type: "python", status: "failed", port: 8000 },
  { domain: "docs.example.com", name: "docs", app_type: "static", status: "static", port: null },
  { domain: "staging.example.com", name: "staging", app_type: "nextjs", status: "deploying", port: 3002 },
  { domain: "blog.example.com", name: "blog", app_type: "vite", status: "stopped", port: 3003 },
  { domain: "status.example.com", name: "status", app_type: "nodejs", status: "running", port: 3004 },
  { domain: "media.example.com", name: "media", app_type: "python", status: "running", port: 8001 },
];

function json(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers });
  res.end(JSON.stringify(body));
}

function fail(
  res: ServerResponse,
  status: number,
  error: string,
  detail: string,
  hint: string | null = null,
  headers: Record<string, string> = {},
): void {
  json(res, status, { error, detail, hint, fields: null }, headers);
}

function cookies(req: IncomingMessage): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of (req.headers.cookie ?? "").split(";")) {
    const index = part.indexOf("=");
    if (index > 0) out[part.slice(0, index).trim()] = decodeURIComponent(part.slice(index + 1).trim());
  }
  return out;
}

async function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  const text = Buffer.concat(chunks).toString("utf8");
  if (text === "") return {};
  try {
    const parsed: unknown = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function iso(seconds: number | null): string | null {
  return seconds === null ? null : new Date(seconds * 1000).toISOString();
}

export function mockBackend(): Plugin {
  const sessions = new Map<string, Session>();
  const totp = process.env.MOCK_TOTP !== "0";
  let failures = 0;
  let lockedUntil = 0;
  const started = Date.now() / 1000 - 12 * 86_400 - 4 * 3_600;
  const loadHistory: number[] = Array.from({ length: 24 }, (_, i) => 0.35 + 0.25 * Math.sin(i / 3) + (i % 5) * 0.03);

  const now = (): number => Date.now() / 1000;

  const current = (req: IncomingMessage): [string, Session] | null => {
    const sid = cookies(req).wasm_session;
    if (sid === undefined) return null;
    const session = sessions.get(sid);
    if (!session || session.expiresAt < now()) return null;
    return [sid, session];
  };

  const machine = () => {
    const t = now();
    const load = 0.42 + 0.3 * Math.sin(t / 40);
    loadHistory.push(Math.max(0, load));
    loadHistory.splice(0, loadHistory.length - 24);
    return {
      hostname: HOSTNAME,
      uptime_s: t - started,
      load: [Number(load.toFixed(2)), 0.38, 0.31],
      load_history: loadHistory.map((v) => Number(v.toFixed(2))),
      cpu_percent: Number((18 + 9 * Math.sin(t / 25)).toFixed(1)),
      memory: { used: 3_380_000_000, total: 8_000_000_000, percent: 42.3 },
      disk: { used: 61_000_000_000, total: 78_000_000_000, percent: 78.2 },
      units: { running: 9, failed: 1, stopped: 2 },
      apps: { running: 4, failed: 1, stopped: 1, static: 1 },
    };
  };

  const appOut = (app: Snapshot) => ({
    domain: app.domain,
    name: app.name,
    app_type: app.app_type,
    status: app.status,
    active: app.status === "running",
    enabled: app.status !== "stopped",
    port: app.port,
    pid: app.status === "running" && app.port !== null ? 41_000 + app.port : null,
    uptime: app.status === "running" ? "3 days" : null,
    path: `/var/www/apps/${app.name}`,
  });

  const handle = async (req: IncomingMessage, res: ServerResponse): Promise<boolean> => {
    const url = new URL(req.url ?? "/", "http://mock");
    const path = url.pathname;
    const method = (req.method ?? "GET").toUpperCase();
    if (!path.startsWith("/api/") && path !== "/events") return false;

    // Everything below needs the same answers the real middleware gives.
    if (path === "/api/auth/session" && method === "GET") {
      const found = current(req);
      json(res, 200, {
        authenticated: found !== null,
        scope: found ? "admin" : null,
        expires_at: found ? iso(found[1].expiresAt) : null,
        elevated_until: found && found[1].elevatedUntil !== null && found[1].elevatedUntil > now() ? iso(found[1].elevatedUntil) : null,
        totp_enabled: totp,
        hostname: HOSTNAME,
        version: VERSION,
        csrf_header: "X-WASM-CSRF",
        csrf_cookie: "wasm_csrf",
      });
      return true;
    }

    if (path === "/api/auth/login" && method === "POST") {
      if (lockedUntil > now()) {
        const remaining = Math.ceil(lockedUntil - now());
        fail(res, 429, "locked_out", `Too many failed attempts. Locked for ${String(remaining)} seconds.`, null, {
          "Retry-After": String(remaining),
        });
        return true;
      }
      const body = await readJson(req);
      const miss = (error: string, detail: (left: number) => string): void => {
        failures += 1;
        if (failures >= MAX_FAILURES) {
          lockedUntil = now() + LOCKOUT_SECONDS;
          failures = 0;
        }
        fail(res, 401, error, detail(Math.max(0, MAX_FAILURES - failures)));
      };
      if (body.token !== TOKEN) {
        miss("invalid_token", (left) => `Invalid token. ${String(left)} attempts remaining.`);
        return true;
      }
      if (totp) {
        const code = typeof body.totp_code === "string" ? body.totp_code.trim() : "";
        if (code === "") {
          fail(res, 401, "totp_required", "Two-factor authentication is enabled. Include totp_code.");
          return true;
        }
        if (code !== CODE) {
          miss("invalid_totp", (left) => `Invalid two-factor code. ${String(left)} attempts remaining.`);
          return true;
        }
      }
      failures = 0;
      const sid = randomBytes(16).toString("hex");
      const csrf = randomBytes(24).toString("base64url");
      sessions.set(sid, { csrf, expiresAt: now() + SESSION_SECONDS, elevatedUntil: null });
      res.setHeader("Set-Cookie", [
        `wasm_session=${sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${String(SESSION_SECONDS)}`,
        `wasm_csrf=${csrf}; Path=/; SameSite=Strict; Max-Age=${String(SESSION_SECONDS)}`,
      ]);
      json(res, 200, { success: true, expires_in: SESSION_SECONDS, csrf_token: csrf, session_token: null });
      return true;
    }

    if (path === "/api/__mock/expire" && method === "POST") {
      sessions.clear();
      json(res, 200, { expired: true });
      return true;
    }

    const found = current(req);
    if (!found) {
      if (path === "/events") {
        res.writeHead(401).end();
        return true;
      }
      fail(res, 401, "unauthorized", "Authentication required");
      return true;
    }
    const [sid, session] = found;

    if (method !== "GET" && req.headers["x-wasm-csrf"] !== session.csrf) {
      fail(
        res,
        403,
        "forbidden",
        "Missing or invalid CSRF token. Send the wasm_csrf cookie value in the X-WASM-CSRF header, or authenticate with a Bearer token.",
      );
      return true;
    }

    if (path === "/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no" });
      res.write(": connected\n\n");
      const send = (name: string, payload: unknown): void => {
        res.write(`event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`);
      };
      const timers = [
        setInterval(() => {
          if (!sessions.has(sid)) {
            res.end();
            return;
          }
          send("machine", machine());
        }, 5_000),
        setInterval(() => {
          send("metrics", { "system.cpu_percent": machine().cpu_percent });
        }, 2_000),
      ];
      req.on("close", () => {
        for (const timer of timers) clearInterval(timer);
      });
      return true;
    }

    if (path === "/api/auth/logout" && method === "POST") {
      sessions.delete(sid);
      res.setHeader("Set-Cookie", ["wasm_session=; Path=/; Max-Age=0", "wasm_csrf=; Path=/; Max-Age=0"]);
      json(res, 200, { success: true, message: "Logged out successfully" });
      return true;
    }

    if (path === "/api/auth/elevate" && method === "POST") {
      const body = await readJson(req);
      const ok = totp ? body.code === CODE : body.token === TOKEN;
      if (!ok) {
        failures += 1;
        fail(
          res,
          401,
          totp ? "invalid_totp" : "invalid_token",
          totp
            ? `Invalid two-factor code. ${String(MAX_FAILURES - failures)} attempts remaining.`
            : `Invalid token. ${String(MAX_FAILURES - failures)} attempts remaining.`,
        );
        return true;
      }
      session.elevatedUntil = now() + 600;
      json(res, 200, { elevated_until: iso(session.elevatedUntil) });
      return true;
    }

    if (path === "/api/auth/ws-ticket" && method === "POST") {
      json(res, 200, { ticket: randomBytes(16).toString("hex"), expires_in: 30 });
      return true;
    }

    if (path === "/api/system/machine" && method === "GET") {
      json(res, 200, machine());
      return true;
    }

    if (path === "/api/apps" && method === "GET") {
      json(res, 200, { apps: APPS.map(appOut), total: APPS.length });
      return true;
    }

    const appMatch = /^\/api\/apps\/([^/]+)$/.exec(path);
    if (appMatch) {
      const domain = decodeURIComponent(appMatch[1] ?? "");
      const app = APPS.find((candidate) => candidate.domain === domain);
      if (!app) {
        fail(res, 404, "not_found", `Application not found: ${domain}`);
        return true;
      }
      if (method === "GET") {
        json(res, 200, appOut(app));
        return true;
      }
      if (method === "DELETE") {
        if (session.elevatedUntil === null || session.elevatedUntil < now()) {
          fail(res, 403, "elevation_required", "Confirm it's you to continue", "POST /api/auth/elevate with your two-factor code, or the master token.");
          return true;
        }
        json(res, 202, { job_id: "mock1234", status: "pending", message: `Deleting ${domain}`, job: {} });
        return true;
      }
    }

    fail(res, 404, "not_found", `The mock backend does not implement ${method} ${path}.`, "Run the console against a real `wasm web` instance for this page.");
    return true;
  };

  return {
    name: "wasm-mock-backend",
    apply: "serve",
    configureServer(server) {
      server.config.logger.info("\n  WASM mock backend: token wasm_mock_token, two-factor code 123456\n");
      server.middlewares.use((req, res, next) => {
        handle(req, res)
          .then((handled) => {
            if (!handled) next();
          })
          .catch((error: unknown) => {
            next(error);
          });
      });
    },
  };
}

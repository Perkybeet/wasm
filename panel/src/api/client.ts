/**
 * The one way the console talks to the API.
 *
 * `api()` is the transport: same-origin cookies, JSON in and out, the CSRF header mirrored
 * from its cookie, and the three answers that are not the caller's business handled here
 * once: a lost session sends the operator to sign in, a destructive action asks them to
 * confirm it's them and is retried once, and every other failure becomes an ApiError.
 *
 * `request()` is the same call typed by the OpenAPI contract (schema.gen.ts): a path that
 * does not exist, a missing path parameter or a wrong body is a compile error, and the
 * response is typed. Endpoints that still answer a bare dict are typed `unknown`; when the
 * backend declares their response model, the generated types sharpen with no change here.
 */

import type { paths } from "./schema.gen";
import { ElevationCancelledError, errorFromResponse, unreachable } from "./errors";

export { ApiError, ElevationCancelledError, isApiError } from "./errors";

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface ApiHooks {
  /** A request came back 401 without being a failed sign-in: the session is gone. */
  onSessionExpired: () => void;
  /**
   * Asks the operator to confirm it's them. Resolves once the session is elevated; rejects
   * (with ElevationCancelledError) when they decline.
   */
  elevate: () => Promise<void>;
}

const DEFAULT_HOOKS: ApiHooks = {
  onSessionExpired: () => undefined,
  elevate: () => Promise.reject(new ElevationCancelledError()),
};

let hooks: ApiHooks = DEFAULT_HOOKS;

/**
 * Installs what the client calls when the session is lost or an action needs elevation.
 * Returns a function that restores the previous hooks (tests, hot reload).
 */
export function configureApi(next: Partial<ApiHooks>): () => void {
  const previous = hooks;
  hooks = { ...hooks, ...next };
  return () => {
    hooks = previous;
  };
}

/** Forgets every installed hook and any pending elevation. For tests. */
export function resetApiHooks(): void {
  hooks = DEFAULT_HOOKS;
  pendingElevation = null;
}

/** Reports a lost session through the installed hook, for callers that learn it another way. */
export function expireSession(): void {
  hooks.onSessionExpired();
}

// The backend names both in GET /api/auth/session; these are its defaults.
const csrf = { header: "X-WASM-CSRF", cookie: "wasm_csrf" };

/** Adopts the CSRF header and cookie names the session endpoint announced. */
export function setCsrfNames(header: string, cookie: string): void {
  csrf.header = header;
  csrf.cookie = cookie;
}

export function readCookie(name: string): string | null {
  for (const part of document.cookie.split(";")) {
    const index = part.indexOf("=");
    if (index === -1) continue;
    if (part.slice(0, index).trim() === name) return decodeURIComponent(part.slice(index + 1).trim());
  }
  return null;
}

export interface ApiInit {
  signal?: AbortSignal | undefined;
}

// Several requests can hit a destructive endpoint at once; the operator confirms once.
let pendingElevation: Promise<void> | null = null;

function elevateOnce(): Promise<void> {
  pendingElevation ??= hooks.elevate().finally(() => {
    pendingElevation = null;
  });
  return pendingElevation;
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (text === "") return undefined;
  const type = response.headers.get("Content-Type") ?? "";
  return type.includes("json") ? (JSON.parse(text) as unknown) : text;
}

async function send(method: Method, path: string, body: unknown, init: ApiInit, mayElevate: boolean): Promise<unknown> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = readCookie(csrf.cookie);
  if (token !== null) headers[csrf.header] = token;

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      ...(init.signal ? { signal: init.signal } : {}),
    });
  } catch (cause: unknown) {
    // A cancelled query is not a failure; TanStack Query expects the abort to propagate.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw unreachable(cause);
  }

  if (response.ok) return readBody(response);

  const error = await errorFromResponse(response);
  if (error.sessionExpired) {
    hooks.onSessionExpired();
  } else if (mayElevate && error.status === 403 && error.error === "elevation_required") {
    await elevateOnce();
    // Exactly one retry: a second refusal is reported, never a second dialog.
    return send(method, path, body, init, false);
  }
  throw error;
}

/**
 * Calls the API and returns the decoded JSON body.
 *
 * @throws ApiError for any non-2xx answer or when the server cannot be reached.
 */
export async function api<T>(method: Method, path: string, body?: unknown, init: ApiInit = {}): Promise<T> {
  return (await send(method, path, body, init, true)) as T;
}

// ---------------------------------------------------------------------------------------
// The typed layer over the OpenAPI contract.

type HttpMethod = "get" | "post" | "put" | "patch" | "delete";
export type ApiPath = keyof paths;

type Operation<P extends ApiPath, M extends HttpMethod> = NonNullable<paths[P][M]>;

/** The methods a path actually declares. */
export type MethodOf<P extends ApiPath> = {
  [M in HttpMethod]: [paths[P][M]] extends [undefined] ? never : M;
}[HttpMethod];

type JsonOf<R> = R extends { content: { "application/json": infer B } } ? B : undefined;

type Success<R> = R extends { 200: infer S }
  ? JsonOf<S>
  : R extends { 201: infer S }
    ? JsonOf<S>
    : R extends { 202: infer S }
      ? JsonOf<S>
      : undefined;

/** The decoded body of a successful call. `unknown` until the backend declares a model. */
export type ResponseOf<P extends ApiPath, M extends MethodOf<P>> = Operation<P, M> extends { responses: infer R }
  ? Success<R>
  : never;

/** The JSON body a call takes, or undefined when it takes none. */
export type BodyOf<P extends ApiPath, M extends MethodOf<P>> = Operation<P, M> extends { requestBody?: never }
  ? undefined
  : Operation<P, M> extends { requestBody?: { content: { "application/json": infer B } } }
    ? B
    : undefined;

/** The query parameters a call accepts. */
export type QueryOf<P extends ApiPath, M extends MethodOf<P>> = Operation<P, M> extends { parameters: { query?: infer Q } }
  ? [Q] extends [undefined]
    ? undefined
    : Q
  : undefined;

type ParamNames<S extends string> = S extends `${string}{${infer Name}}${infer Rest}` ? Name | ParamNames<Rest> : never;

type PathParams<P extends string> = [ParamNames<P>] extends [never]
  ? undefined
  : Record<ParamNames<P>, string | number>;

export type RequestOptions<P extends ApiPath, M extends MethodOf<P>> = (PathParams<P> extends undefined
  ? { params?: undefined }
  : { params: PathParams<P> }) &
  (BodyOf<P, M> extends undefined ? { body?: undefined } : { body: BodyOf<P, M> }) &
  (QueryOf<P, M> extends undefined ? { query?: undefined } : { query?: QueryOf<P, M> }) & { signal?: AbortSignal };

type OptionsArg<P extends ApiPath, M extends MethodOf<P>> = object extends RequestOptions<P, M>
  ? [options?: RequestOptions<P, M>]
  : [options: RequestOptions<P, M>];

type QueryValue = string | number | boolean | null | undefined | readonly (string | number | boolean)[];

/** Fills `{name}` segments (encoded) and appends the query string, skipping empty values. */
export function buildPath(
  template: string,
  params?: Readonly<Record<string, string | number>>,
  query?: Readonly<Record<string, QueryValue>>,
): string {
  const path = template.replace(/\{([^}]+)\}/g, (_, name: string) => {
    const value = params?.[name];
    if (value === undefined) throw new Error(`Missing path parameter "${name}" for ${template}`);
    return encodeURIComponent(String(value));
  });
  if (!query) return path;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) {
      for (const item of value as readonly (string | number | boolean)[]) search.append(key, String(item));
    } else {
      search.append(key, String(value));
    }
  }
  const qs = search.toString();
  return qs === "" ? path : `${path}?${qs}`;
}

/**
 * Calls an endpoint of the OpenAPI contract.
 *
 *     request("get", "/api/apps/{domain}", { params: { domain } })
 */
export function request<P extends ApiPath, M extends MethodOf<P>>(
  method: M,
  path: P,
  ...[options]: OptionsArg<P, M>
): Promise<ResponseOf<P, M>> {
  const opts = (options ?? {}) as {
    params?: Record<string, string | number>;
    query?: Record<string, QueryValue>;
    body?: unknown;
    signal?: AbortSignal;
  };
  const url = buildPath(path, opts.params, opts.query);
  return api<ResponseOf<P, M>>(method.toUpperCase() as Method, url, opts.body, { signal: opts.signal });
}

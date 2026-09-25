/**
 * The API's error contract, as the console receives it.
 *
 * Every route under /api answers a failure as `{error, detail, hint, fields}` (the backend's
 * `wasm.web.api.deps.ErrorResponse`). `error` is the machine-readable code the console
 * branches on; `detail` is the system's own words and is shown verbatim; `hint` is the fix,
 * shown above it; `fields` maps a form field to its validation message.
 */

/**
 * 401 codes that mean "these credentials were wrong", as opposed to "there is no session".
 * The login and elevate endpoints answer them; a 401 carrying anything else means the
 * session is gone and the operator has to sign in again.
 */
export const CREDENTIAL_ERRORS: ReadonlySet<string> = new Set(["invalid_token", "totp_required", "invalid_totp"]);

/** A request the API refused or could not answer. */
export class ApiError extends Error {
  /** HTTP status; 0 when the server could not be reached at all. */
  readonly status: number;
  /** Machine-readable code: `validation_error`, `elevation_required`, `locked_out`... */
  readonly error: string;
  /** What went wrong, in the system's own words. Never paraphrase it. */
  readonly detail: string;
  /** How to fix it, when the backend knows. */
  readonly hint: string | null;
  /** Validation message per field name, for a 422. */
  readonly fields: Readonly<Record<string, string>> | null;
  /** Seconds the server asked the client to wait (Retry-After), for a 429. */
  readonly retryAfter: number | null;

  constructor(
    status: number,
    error: string,
    detail: string,
    hint: string | null = null,
    fields: Record<string, string> | null = null,
    retryAfter: number | null = null,
  ) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.error = error;
    this.detail = detail;
    this.hint = hint;
    this.fields = fields;
    this.retryAfter = retryAfter;
  }

  /** True when the session is gone, not when a typed credential was wrong. */
  get sessionExpired(): boolean {
    return this.status === 401 && !CREDENTIAL_ERRORS.has(this.error);
  }
}

/** The operator closed "Confirm it's you" instead of confirming: the action did not run. */
export class ElevationCancelledError extends ApiError {
  constructor() {
    super(
      403,
      "elevation_cancelled",
      "Nothing was changed because the confirmation was cancelled.",
      "Run the action again and confirm it's you to continue.",
    );
    this.name = "ElevationCancelledError";
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/** The fallback code for a response that did not carry one, mirroring the backend's table. */
const CODE_BY_STATUS: Readonly<Record<number, string>> = {
  400: "validation_error",
  401: "unauthorized",
  403: "forbidden",
  404: "not_found",
  409: "conflict",
  422: "validation_error",
  429: "rate_limited",
};

function codeFor(status: number): string {
  return CODE_BY_STATUS[status] ?? (status >= 500 ? "internal" : "http_error");
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function fieldMap(value: unknown): Record<string, string> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const entries = Object.entries(value as Record<string, unknown>).filter(
    (entry): entry is [string, string] => typeof entry[1] === "string",
  );
  return entries.length > 0 ? Object.fromEntries(entries) : null;
}

function retryAfterSeconds(response: Response): number | null {
  const header = response.headers.get("Retry-After");
  if (header === null) return null;
  const seconds = Number.parseInt(header, 10);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

/**
 * Builds an ApiError from a failed response. Anything that is not the contract (a proxy's
 * HTML error page, an empty 502) is still reported with its body verbatim, so the operator
 * sees what the proxy said instead of a generic message.
 */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  const text = await response.text().catch(() => "");
  const retryAfter = retryAfterSeconds(response);
  let body: unknown;
  try {
    body = text === "" ? null : JSON.parse(text);
  } catch {
    body = null;
  }

  if (typeof body === "object" && body !== null && !Array.isArray(body)) {
    const record = body as Record<string, unknown>;
    // FastAPI's own shape outside the contract: {"detail": "..."} or a list of problems.
    const detail =
      stringOrNull(record["detail"]) ??
      (record["detail"] !== undefined ? JSON.stringify(record["detail"]) : null) ??
      `${String(response.status)} ${response.statusText}`.trim();
    return new ApiError(
      response.status,
      stringOrNull(record["error"]) ?? codeFor(response.status),
      detail,
      stringOrNull(record["hint"]),
      fieldMap(record["fields"]),
      retryAfter,
    );
  }

  const detail = text.trim() !== "" ? text.trim() : `${String(response.status)} ${response.statusText}`.trim();
  return new ApiError(response.status, codeFor(response.status), detail, null, null, retryAfter);
}

/** The server did not answer at all: it is down, restarting, or the network is gone. */
export function unreachable(cause: unknown): ApiError {
  const detail = cause instanceof Error && cause.message !== "" ? cause.message : "The request did not reach the server.";
  return new ApiError(
    0,
    "network",
    detail,
    "The console could not reach the WASM panel. Check that it is running with `wasm web status`.",
  );
}

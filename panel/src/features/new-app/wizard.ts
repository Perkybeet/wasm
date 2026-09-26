/**
 * The new-app wizard's logic, apart from any page: what the operator pointed at, the form the
 * inspection proposes, what is wrong with it, and the request it becomes. Pure, so every rule
 * is tested without rendering a step.
 */

import { isApiError } from "../../api/client";
import type { BodyOf, ResponseOf } from "../../api/client";
import { draftOf, parseLimits } from "../app/settings/limits";
import type { LimitsDraft } from "../app/settings/limits";
import { domainProblem, normalizeDomain } from "../domains/names";

export type Inspection = ResponseOf<"/api/apps/inspect", "post">;
export type EnvKey = Inspection["env_keys"][number];
export type CreateAppBody = BodyOf<"/api/apps", "post">;
export type AppTypeOption = ResponseOf<"/api/apps/types", "get">["types"][number];

export type Step = "source" | "review" | "deploy";

export const STEPS: readonly { id: Step; label: string }[] = [
  { id: "source", label: "Source" },
  { id: "review", label: "Review" },
  { id: "deploy", label: "Deploy" },
];

// ---------------------------------------------------------------------------------------
// The source

export type SourceKind = "git" | "archive" | "local" | "unknown";

/**
 * What a source looks like, for the hint under the field. The server decides for real
 * (`validate_source`); this only tells the operator what WASM will do with it.
 */
export function sourceKind(value: string): SourceKind {
  const source = value.trim();
  if (source === "") return "unknown";
  if (source.startsWith("/") || source.startsWith("~/") || source.startsWith("./") || source.startsWith("../")) return "local";
  if (/^(?:https?|ftp):\/\/\S+\.(?:zip|tar\.gz|tgz|tar\.bz2|tar\.xz|tar)(?:\?\S*)?$/i.test(source)) return "archive";
  if (/^(?:https?|ssh|git):\/\/\S+$/i.test(source) || /^[\w.-]+@[\w.-]+:\S+$/.test(source)) return "git";
  return "unknown";
}

/**
 * A source in a few characters, for places too narrow for all of it: the end of a path
 * (`src/storefront`), the repository of a URL (`you/app`).
 */
export function shortSource(value: string): string {
  const source = value.trim().replace(/\/+$/, "").replace(/\.git$/, "");
  const parts = source.split(/[/:]/).filter((part) => part !== "");
  return parts.slice(-2).join("/") || source;
}

export const SOURCE_WORDS: Record<SourceKind, string> = {
  git: "Git repository: cloned at the branch below, or its default branch.",
  archive: "Archive: downloaded and unpacked.",
  local: "Directory on this server: copied as it is, without .git, node_modules or virtualenvs.",
  unknown: "A Git URL (https or ssh), an archive URL, or an absolute path on this server.",
};

export interface SourceForm {
  source: string;
  branch: string;
}

export type SourceErrors = Partial<Record<"source" | "branch", string>>;

export function sourceProblems(form: SourceForm): SourceErrors {
  const errors: SourceErrors = {};
  if (form.source.trim() === "") errors.source = "Enter a Git URL or a path on this server.";
  else if (sourceKind(form.source) === "unknown") errors.source = "This is neither a URL nor an absolute path. Paths on this server start with /.";
  if (form.branch.trim() !== "" && !/^[\w./-]+$/.test(form.branch.trim())) {
    errors.branch = "A branch name uses letters, digits, dots, slashes, hyphens and underscores.";
  }
  return errors;
}

/**
 * What the Review step starts from when detection found nothing the operator wants: no type,
 * no commands, no variables. The type is then chosen by hand, and its deployer decides the
 * rest when it runs.
 */
export function manualInspection(source: SourceForm): Inspection {
  return {
    app_type: "",
    detected_types: [],
    package_manager: null,
    install_command: [],
    build_command: [],
    start_command: "",
    default_port: 3000,
    env_keys: [],
    branch: source.branch.trim(),
    commit: "",
  };
}

// ---------------------------------------------------------------------------------------
// The review

/**
 * The registry's display name (`DISPLAY_NAME`) for a type, from `GET /api/apps/types` - the
 * one source of truth for what WASM can deploy (`available_types`). Falls back to the raw
 * identifier while the list has not loaded yet, or for a type the wizard has not seen.
 */
export function typeName(types: readonly AppTypeOption[], type: string): string {
  return types.find((entry) => entry.type === type)?.name ?? type;
}

/**
 * Every type the operator can choose, the detected ones first in the order the registry
 * matched them, then the rest as the API ordered them (alphabetical, `auto` last). The first
 * is what WASM would deploy as.
 */
export function typeOptions(types: readonly AppTypeOption[], detected: readonly string[]): { value: string; label: string; hint?: string }[] {
  const rest = types.filter((entry) => !detected.includes(entry.type));
  return [
    ...detected.map((type, index) => ({
      value: type,
      label: typeName(types, type),
      hint: index === 0 ? "Detected, the closest match" : "Also matches this repository",
    })),
    ...rest.map((entry) => ({ value: entry.type, label: entry.name })),
  ];
}

/** Types that run no process of their own, so they have no port. */
export function hasPort(type: string): boolean {
  return type !== "static";
}

export interface EnvRow {
  /** Stable across edits, for React keys and error names. */
  id: string;
  name: string;
  value: string;
  secret: boolean;
  required: boolean;
  /** Declared in .env.example: its name is fixed, only its value is edited. */
  declared: boolean;
  /** The value .env.example gave it, if any. */
  example: string | null;
}

export type Layout = "releases" | "inplace";
export type WebServer = "nginx" | "apache";

export interface PathRow {
  /** Stable across edits, for React keys and error names. */
  id: string;
  value: string;
}

export interface ReviewForm {
  appType: string;
  domain: string;
  /** Also answer on www.<domain>, as a redirect to it. Only offered where that means anything. */
  includeWww: boolean;
  webserver: WebServer;
  ssl: boolean;
  port: string;
  layout: Layout;
  /** Releases only: paths kept in shared/ and linked into every release. */
  persistentPaths: PathRow[];
  limits: LimitsDraft;
  env: EnvRow[];
}

/**
 * Whether "Also serve www" would do anything for this domain: `should_include_www` on the
 * server folds it to false for a subdomain or a name that already is `www.*`, and offering a
 * toggle that a deploy would silently ignore is worse than not offering it.
 */
export function canIncludeWww(domain: string): boolean {
  const parts = normalizeDomain(domain).split(".");
  return parts.length === 2 && parts[0] !== "www";
}

export function envRowsFrom(keys: readonly EnvKey[]): EnvRow[] {
  return keys.map((key) => ({
    id: `declared:${key.name}`,
    name: key.name,
    value: key.default ?? "",
    secret: key.secret,
    required: key.required,
    declared: true,
    example: key.default ?? null,
  }));
}

/**
 * The port to propose: the type's own default, or the first one after it no app on this
 * machine has taken. Proposing a port another app holds would only earn an error.
 */
export function proposedPort(preferred: number, taken: ReadonlyMap<number, string>): number {
  let port = preferred;
  while (taken.has(port) && port < 65535) port += 1;
  return port;
}

/**
 * The form an inspection proposes. When the operator inspects again (another branch, a fixed
 * repository), what they already typed is kept: the domain, the choices, and the value of
 * every variable that is still declared. Nothing is asked twice.
 */
export function initialReview(
  inspection: Inspection,
  defaults: { webserver: WebServer; taken: ReadonlyMap<number, string> },
  previous?: ReviewForm | null,
): ReviewForm {
  const env = envRowsFrom(inspection.env_keys);
  if (!previous) {
    return {
      appType: inspection.app_type,
      domain: "",
      includeWww: false,
      webserver: defaults.webserver,
      ssl: true,
      port: String(proposedPort(inspection.default_port, defaults.taken)),
      layout: "releases",
      persistentPaths: [],
      limits: draftOf({}),
      env,
    };
  }
  const typed = new Map(previous.env.map((row) => [row.name, row]));
  const kept = env.map((row) => {
    const before = typed.get(row.name);
    return before ? { ...row, value: before.value } : row;
  });
  const extra = previous.env.filter((row) => !row.declared && !env.some((declared) => declared.name === row.name));
  return {
    ...previous,
    appType: inspection.detected_types.includes(previous.appType) ? previous.appType : inspection.app_type,
    env: [...kept, ...extra],
  };
}

export interface ReviewContext {
  /** Domains already deployed on this machine. */
  domains: ReadonlySet<string>;
  /** Ports already taken by an app, and by which. */
  ports: ReadonlyMap<number, string>;
  /** CPUs of this machine, for the CPU quota's upper bound; null while unknown. */
  cores: number | null;
}

export type ReviewErrors = Record<string, string>;

const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** Field name of a variable's value in ReviewErrors. */
export function envField(row: Pick<EnvRow, "id">): string {
  return `env:${row.id}`;
}

/** Field name of an added variable's name in ReviewErrors. */
export function envNameField(row: Pick<EnvRow, "id">): string {
  return `env-name:${row.id}`;
}

/** Field name of a persistent path in ReviewErrors. */
export function pathField(row: Pick<PathRow, "id">): string {
  return `path:${row.id}`;
}

/** Field name of a resource limit in ReviewErrors. */
export function limitField(name: keyof LimitsDraft): string {
  return `limit:${name}`;
}

/**
 * A persistent path from the operator, or why the deployer would refuse it - the same check
 * `wasm.deployers.releases.persistent_path` runs when the deploy links `shared/`, run here
 * first so a typo is caught before the build rather than after.
 */
export function persistentPathProblem(raw: string): string | null {
  const value = raw.trim();
  if (value === "") return "Enter a path, such as storage or public/uploads.";
  if (value.startsWith("/") || value.split("/").some((part) => part === "..")) {
    return "A persistent path is relative to the application, such as storage or public/uploads, and cannot start with / or contain '..'.";
  }
  return null;
}

/** The port the operator typed, or why it is not one the server will take. */
export function portProblem(value: string, taken: ReadonlyMap<number, string>): string | null {
  const text = value.trim();
  if (!/^\d+$/.test(text)) return "Enter a port number, such as 3000.";
  const port = Number(text);
  if (port < 1 || port > 65535) return "A port is between 1 and 65535.";
  if (port < 1024 && port !== 80 && port !== 443) return "Ports below 1024 are reserved for the system. Use 1024 or above.";
  const owner = taken.get(port);
  if (owner !== undefined) return `Port ${text} is used by ${owner}.`;
  return null;
}

export function reviewProblems(form: ReviewForm, context: ReviewContext): ReviewErrors {
  const errors: ReviewErrors = {};
  if (form.appType === "") errors["appType"] = "Choose how to deploy it: the type decides how it is installed, built and started.";
  const domain = normalizeDomain(form.domain);
  const bad = domainProblem(domain);
  if (bad !== null) errors["domain"] = bad;
  else if (context.domains.has(domain)) errors["domain"] = `${domain} is already deployed. Choose another domain, or update that app from its page.`;

  if (hasPort(form.appType)) {
    const port = portProblem(form.port, context.ports);
    if (port !== null) errors["port"] = port;
  }

  const names = new Set<string>();
  for (const row of form.env) {
    const name = row.name.trim();
    if (!row.declared) {
      if (name === "" && row.value === "") continue;
      if (!ENV_NAME.test(name)) {
        errors[envNameField(row)] = "A name uses letters, digits and underscores, and does not start with a digit.";
        continue;
      }
    }
    if (names.has(name)) errors[envNameField(row)] = `${name} is set twice.`;
    names.add(name);
    if (row.required && row.value.trim() === "") {
      errors[envField(row)] = ".env.example gives it no value, so the app expects one.";
    }
  }

  if (form.layout === "releases") {
    const paths = new Set<string>();
    for (const row of form.persistentPaths) {
      const value = row.value.trim();
      if (value === "") continue;
      const problem = persistentPathProblem(value);
      if (problem !== null) {
        errors[pathField(row)] = problem;
        continue;
      }
      if (paths.has(value)) {
        errors[pathField(row)] = `${value} is listed twice.`;
        continue;
      }
      paths.add(value);
    }
  }

  const limitErrors = parseLimits(form.limits, context.cores).errors;
  for (const name of Object.keys(limitErrors) as (keyof LimitsDraft)[]) {
    const message = limitErrors[name];
    if (message !== undefined) errors[limitField(name)] = message;
  }

  return errors;
}

/** The request `POST /api/apps` takes, from what the operator reviewed. */
export function createAppBody(source: SourceForm, form: ReviewForm): CreateAppBody {
  const env: Record<string, string> = {};
  for (const row of form.env) {
    const name = row.name.trim();
    if (name === "") continue;
    env[name] = row.value;
  }
  const branch = source.branch.trim();
  const paths = form.layout === "releases" ? form.persistentPaths.map((row) => row.value.trim()).filter((value) => value !== "") : [];
  const limits = parseLimits(form.limits, null).values;
  return {
    domain: normalizeDomain(form.domain),
    source: source.source.trim(),
    ...(branch !== "" && sourceKind(source.source) !== "local" ? { branch } : {}),
    app_type: form.appType,
    ...(hasPort(form.appType) ? { port: Number(form.port.trim()) } : {}),
    webserver: form.webserver,
    ssl: form.ssl,
    layout: form.layout,
    include_www: form.includeWww && canIncludeWww(form.domain),
    ...(paths.length > 0 ? { persistent_paths: paths } : {}),
    memory_max_mb: limits.memory_max_mb,
    cpu_quota_percent: limits.cpu_quota_percent,
    tasks_max: limits.tasks_max,
    env_vars: env,
    skip_database: false,
  };
}

// ---------------------------------------------------------------------------------------
// Refusals

export interface Refusal {
  /** The step the operator has to go back to. */
  step: Step;
  /** Messages per field of that step. Empty when the refusal is about no field in particular. */
  fields: Record<string, string>;
}

/** Fields of the request, by the step that asks for them. */
const SOURCE_FIELDS = new Set(["source", "branch"]);
const REVIEW_FIELDS: Readonly<Record<string, string>> = {
  domain: "domain",
  port: "port",
  app_type: "appType",
  webserver: "webserver",
  ssl: "ssl",
  layout: "layout",
  include_www: "includeWww",
  persistent_paths: "persistentPaths",
  memory_max_mb: limitField("memory"),
  cpu_quota_percent: limitField("cpu"),
  tasks_max: limitField("tasks"),
};

/**
 * Where a refusal of `POST /api/apps` sends the operator: to the step holding the fields a 422
 * names, to the domain for a clash or a bad domain, to the port for a port the machine refuses,
 * to the source when the source is the problem. Anything else stays on the Deploy step.
 */
export function refusalOf(error: unknown): Refusal | null {
  if (!isApiError(error)) return null;
  if (error.fields !== null) {
    const source: Record<string, string> = {};
    const review: Record<string, string> = {};
    for (const [name, message] of Object.entries(error.fields)) {
      if (SOURCE_FIELDS.has(name)) source[name] = message;
      else review[REVIEW_FIELDS[name] ?? name] = message;
    }
    if (Object.keys(source).length > 0) return { step: "source", fields: source };
    return { step: "review", fields: review };
  }
  if (error.status === 409 || error.error === "domainerror") return { step: "review", fields: { domain: error.detail } };
  if (error.error === "porterror") return { step: "review", fields: { port: error.detail } };
  if (error.error === "sourceerror") return { step: "source", fields: { source: error.detail } };
  return null;
}

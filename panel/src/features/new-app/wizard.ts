/**
 * The new-app wizard's logic, apart from any page: what the operator pointed at, the form the
 * inspection proposes, what is wrong with it, and the request it becomes. Pure, so every rule
 * is tested without rendering a step.
 */

import { isApiError } from "../../api/client";
import type { BodyOf, ResponseOf } from "../../api/client";
import { domainProblem, normalizeDomain } from "../domains/names";

export type Inspection = ResponseOf<"/api/apps/inspect", "post">;
export type EnvKey = Inspection["env_keys"][number];
export type CreateAppBody = BodyOf<"/api/apps", "post">;

/**
 * What the deployers take that `POST /api/apps` does not accept yet: the `www` redirect,
 * persistent paths and resource limits. The wizard does not offer what the API would ignore;
 * the day the API takes one, this stops compiling, and the Review step should offer it.
 */
type NotAcceptedYet = "include_www" | "persistent_paths" | "memory_max_mb" | "cpu_quota_percent" | "tasks_max";
type Assert<T extends true> = T;
export type CreateAppFieldsNotOfferedYet = Assert<[Extract<keyof CreateAppBody, NotAcceptedYet>] extends [never] ? true : false>;

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

/** The registry's display names (`DISPLAY_NAME`), for the types it knows today. */
const TYPE_NAMES: Readonly<Record<string, string>> = {
  nextjs: "Next.js",
  nodejs: "Node.js",
  vite: "Vite",
  python: "Python",
  static: "Static site",
  monorepo: "Monorepo",
  "docker-compose": "Docker Compose",
};

export function typeName(type: string): string {
  return TYPE_NAMES[type] ?? type;
}

/**
 * Every type the operator can choose, the detected ones first in the order the registry
 * matched them, then the rest. The first is what WASM would deploy as.
 */
export function typeOptions(detected: readonly string[]): { value: string; label: string; hint?: string }[] {
  const rest = Object.keys(TYPE_NAMES).filter((type) => !detected.includes(type));
  return [
    ...detected.map((type, index) => ({
      value: type,
      label: typeName(type),
      hint: index === 0 ? "Detected, the closest match" : "Also matches this repository",
    })),
    ...rest.map((type) => ({ value: type, label: typeName(type) })),
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

export interface ReviewForm {
  appType: string;
  domain: string;
  webserver: WebServer;
  ssl: boolean;
  port: string;
  layout: Layout;
  env: EnvRow[];
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
      webserver: defaults.webserver,
      ssl: true,
      port: String(proposedPort(inspection.default_port, defaults.taken)),
      layout: "releases",
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
  return {
    domain: normalizeDomain(form.domain),
    source: source.source.trim(),
    ...(branch !== "" && sourceKind(source.source) !== "local" ? { branch } : {}),
    app_type: form.appType,
    ...(hasPort(form.appType) ? { port: Number(form.port.trim()) } : {}),
    webserver: form.webserver,
    ssl: form.ssl,
    layout: form.layout,
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

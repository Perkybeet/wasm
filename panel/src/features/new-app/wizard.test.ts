import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/errors";
import { generateSecret } from "./secrets";
import {
  canIncludeWww,
  createAppBody,
  initialReview,
  manualInspection,
  persistentPathProblem,
  proposedPort,
  refusalOf,
  reviewProblems,
  shortSource,
  sourceKind,
  sourceProblems,
  typeName,
  typeOptions,
} from "./wizard";
import type { AppTypeOption, Inspection, PathRow, ReviewForm } from "./wizard";

const INSPECTION: Inspection = {
  app_type: "nextjs",
  detected_types: ["nextjs", "nodejs"],
  package_manager: "npm",
  install_command: ["npm", "ci"],
  build_command: ["npm", "run", "build"],
  start_command: "npm run start",
  default_port: 3000,
  env_keys: [
    { name: "DATABASE_URL", default: null, secret: false, required: true },
    { name: "NEXTAUTH_SECRET", default: null, secret: true, required: true },
    { name: "LOG_LEVEL", default: "info", secret: false, required: false },
  ],
  branch: "main",
  commit: "a1b2c3d",
};

const TYPES: AppTypeOption[] = [
  { type: "docker-compose", name: "Docker Compose", default_port: 3000 },
  { type: "monorepo", name: "Monorepo (Turborepo/pnpm)", default_port: 3000 },
  { type: "nextjs", name: "Next.js", default_port: 3000 },
  { type: "nodejs", name: "Node.js", default_port: 3000 },
  { type: "python", name: "Python (Django/Flask/FastAPI)", default_port: 8000 },
  { type: "static", name: "Static Site", default_port: 80 },
  { type: "vite", name: "Vite (React/Vue/Svelte)", default_port: 5173 },
  { type: "auto", name: "Auto-detect", default_port: 3000 },
];

const NOBODY = { domains: new Set<string>(), ports: new Map<number, string>(), cores: null };

function review(patch: Partial<ReviewForm> = {}): ReviewForm {
  return { ...initialReview(INSPECTION, { webserver: "nginx", taken: new Map() }), domain: "shop.example.com", ...patch };
}

function path(id: string, value: string): PathRow {
  return { id, value };
}

describe("the source", () => {
  it("tells a Git URL, an archive and a directory apart", () => {
    expect(sourceKind("https://github.com/you/app.git")).toBe("git");
    expect(sourceKind("git@github.com:you/app.git")).toBe("git");
    expect(sourceKind("ssh://git@host/you/app")).toBe("git");
    expect(sourceKind("https://example.com/releases/app-1.2.tar.gz")).toBe("archive");
    expect(sourceKind("/var/www/src/storefront")).toBe("local");
    expect(sourceKind("~/code/app")).toBe("local");
    expect(sourceKind("storefront")).toBe("unknown");
    expect(sourceKind("")).toBe("unknown");
  });

  it("asks for something that can be fetched, and a branch that is a branch", () => {
    expect(sourceProblems({ source: "", branch: "" })).toEqual({ source: expect.stringMatching(/Enter a Git URL/) as string });
    expect(sourceProblems({ source: "storefront", branch: "" })).toEqual({ source: expect.stringMatching(/start with \//) as string });
    expect(sourceProblems({ source: "/srv/app", branch: "feature/x y" })).toEqual({ branch: expect.stringMatching(/branch name/) as string });
    expect(sourceProblems({ source: "https://github.com/you/app.git", branch: "release/1.4" })).toEqual({});
  });

  it("shortens a source to its last two parts", () => {
    expect(shortSource("/tmp/wasm-console-x/var/www/src/storefront/")).toBe("src/storefront");
    expect(shortSource("https://github.com/you/app.git")).toBe("you/app");
    expect(shortSource("git@github.com:you/app.git")).toBe("you/app");
  });
});

describe("the review the inspection proposes", () => {
  it("lists the detected types first, the closest match on top, then every other type as the API ordered them", () => {
    const options = typeOptions(TYPES, ["nextjs", "nodejs"]);
    expect(options.slice(0, 2)).toEqual([
      { value: "nextjs", label: "Next.js", hint: "Detected, the closest match" },
      { value: "nodejs", label: "Node.js", hint: "Also matches this repository" },
    ]);
    expect(options.map((option) => option.value)).toEqual(expect.arrayContaining(["vite", "python", "static", "monorepo", "docker-compose", "auto"]));
    expect(new Set(options.map((option) => option.value)).size).toBe(options.length);
  });

  it("names a type from the API's list, and falls back to the raw identifier for one it has not seen", () => {
    expect(typeName(TYPES, "nextjs")).toBe("Next.js");
    expect(typeName(TYPES, "static")).toBe("Static Site");
    expect(typeName([], "nextjs")).toBe("nextjs");
  });

  it("starts from the detected type, releases, HTTPS, no www, no persistent paths and no limits", () => {
    const form = initialReview(INSPECTION, { webserver: "apache", taken: new Map() });
    expect(form).toMatchObject({
      appType: "nextjs",
      domain: "",
      includeWww: false,
      webserver: "apache",
      ssl: true,
      port: "3000",
      layout: "releases",
      persistentPaths: [],
      limits: { memory: "", cpu: "", tasks: "" },
    });
    expect(form.env.map((row) => [row.name, row.value, row.required, row.secret])).toEqual([
      ["DATABASE_URL", "", true, false],
      ["NEXTAUTH_SECRET", "", true, true],
      ["LOG_LEVEL", "info", false, false],
    ]);
  });

  it("proposes the next free port when another app holds the default", () => {
    const taken = new Map([
      [3000, "shop.example.com"],
      [3001, "admin.example.com"],
    ]);
    expect(proposedPort(3000, taken)).toBe(3002);
    expect(initialReview(INSPECTION, { webserver: "nginx", taken }).port).toBe("3002");
  });

  it("keeps what the operator typed when the source is inspected again, www and limits included", () => {
    const typed = review({
      appType: "nodejs",
      port: "4100",
      includeWww: true,
      limits: { memory: "512", cpu: "", tasks: "" },
      persistentPaths: [path("path:1", "storage")],
      env: [...review().env],
    });
    typed.env = typed.env.map((row) => (row.name === "DATABASE_URL" ? { ...row, value: "postgres://db" } : row));
    typed.env.push({ id: "added:1", name: "EXTRA", value: "1", secret: false, required: false, declared: false, example: null });
    const again = initialReview({ ...INSPECTION, env_keys: INSPECTION.env_keys.slice(0, 1) }, { webserver: "apache", taken: new Map() }, typed);
    expect(again).toMatchObject({
      appType: "nodejs",
      domain: "shop.example.com",
      port: "4100",
      webserver: "nginx",
      includeWww: true,
      limits: { memory: "512", cpu: "", tasks: "" },
      persistentPaths: [{ id: "path:1", value: "storage" }],
    });
    expect(again.env.map((row) => [row.name, row.value])).toEqual([
      ["DATABASE_URL", "postgres://db"],
      ["EXTRA", "1"],
    ]);
  });

  it("starts with no type when the operator chooses it by hand", () => {
    const form = initialReview(manualInspection({ source: "/srv/app", branch: "" }), { webserver: "nginx", taken: new Map() });
    expect(form.appType).toBe("");
    expect(reviewProblems({ ...form, domain: "a.example.com" }, NOBODY)["appType"]).toMatch(/Choose how to deploy it/);
  });
});

describe("also serving www", () => {
  it("offers it only for a bare two-label domain that is not already www", () => {
    expect(canIncludeWww("example.com")).toBe(true);
    expect(canIncludeWww("Example.COM")).toBe(true);
    expect(canIncludeWww("shop.example.com")).toBe(false);
    expect(canIncludeWww("www.example.com")).toBe(false);
    expect(canIncludeWww("localhost")).toBe(false);
  });
});

describe("persistent paths", () => {
  it("wants a path relative to the application, without '..' or a leading slash", () => {
    expect(persistentPathProblem("")).toMatch(/Enter a path/);
    expect(persistentPathProblem("/etc/passwd")).toMatch(/relative to the application/);
    expect(persistentPathProblem("../secrets")).toMatch(/relative to the application/);
    expect(persistentPathProblem("storage")).toBeNull();
    expect(persistentPathProblem("public/uploads")).toBeNull();
  });
});

describe("what is wrong with the review", () => {
  it("wants a domain nobody deployed, a usable port and the required variables", () => {
    const problems = reviewProblems(review({ domain: "shop.example.com", port: "3000" }), {
      domains: new Set(["shop.example.com"]),
      ports: new Map([[3000, "admin.example.com"]]),
      cores: null,
    });
    expect(problems["domain"]).toMatch(/already deployed/);
    expect(problems["port"]).toBe("Port 3000 is used by admin.example.com.");
    expect(problems["env:declared:DATABASE_URL"]).toMatch(/expects one/);
    expect(problems["env:declared:NEXTAUTH_SECRET"]).toMatch(/expects one/);
    expect(problems["env:declared:LOG_LEVEL"]).toBeUndefined();
  });

  it("refuses ports the server refuses, and none for a static site", () => {
    expect(reviewProblems(review({ port: "80" }), NOBODY)["port"]).toBeUndefined();
    expect(reviewProblems(review({ port: "22" }), NOBODY)["port"]).toMatch(/below 1024/);
    expect(reviewProblems(review({ port: "abc" }), NOBODY)["port"]).toMatch(/port number/);
    expect(reviewProblems(review({ port: "", appType: "static" }), NOBODY)["port"]).toBeUndefined();
  });

  it("checks added variables' names and ignores an empty added row", () => {
    const form = review();
    form.env = [
      ...form.env.map((row) => ({ ...row, value: "x" })),
      { id: "added:1", name: "1BAD", value: "x", secret: false, required: false, declared: false, example: null },
      { id: "added:2", name: "", value: "", secret: false, required: false, declared: false, example: null },
      { id: "added:3", name: "LOG_LEVEL", value: "debug", secret: false, required: false, declared: false, example: null },
    ];
    expect(reviewProblems(form, NOBODY)).toEqual({
      "env-name:added:1": expect.stringMatching(/letters, digits and underscores/) as string,
      "env-name:added:3": "LOG_LEVEL is set twice.",
    });
  });

  it("checks persistent paths only on the releases layout, and ignores an empty row", () => {
    const releases = review({
      layout: "releases",
      persistentPaths: [path("path:1", ""), path("path:2", "../etc"), path("path:3", "storage"), path("path:4", "storage")],
    });
    const problems = reviewProblems(releases, NOBODY);
    expect(problems["path:path:1"]).toBeUndefined();
    expect(problems["path:path:2"]).toMatch(/relative to the application/);
    expect(problems["path:path:3"]).toBeUndefined();
    expect(problems["path:path:4"]).toBe("storage is listed twice.");

    const inplace = review({ layout: "inplace", persistentPaths: [path("path:1", "../etc")] });
    expect(reviewProblems(inplace, NOBODY)).not.toHaveProperty("path:path:1");
  });

  it("checks resource limits with the same bounds the app's Settings limits use", () => {
    const tooSmall = review({ limits: { memory: "32", cpu: "", tasks: "" } });
    expect(reviewProblems(tooSmall, NOBODY)["limit:memory"]).toMatch(/too small/);

    const tooMuchCpu = review({ limits: { memory: "", cpu: "500", tasks: "" } });
    expect(reviewProblems(tooMuchCpu, { ...NOBODY, cores: 2 })["limit:cpu"]).toMatch(/This machine has 2 CPUs/);
    expect(reviewProblems(tooMuchCpu, NOBODY)["limit:cpu"]).toBeUndefined();

    const notANumber = review({ limits: { memory: "", cpu: "", tasks: "abc" } });
    expect(reviewProblems(notANumber, NOBODY)["limit:tasks"]).toMatch(/whole number/);

    expect(reviewProblems(review({ limits: { memory: "512", cpu: "50", tasks: "256" } }), { ...NOBODY, cores: 4 })).not.toHaveProperty("limit:memory");
  });
});

describe("the request", () => {
  it("is exactly what POST /api/apps takes, www and empty limits included", () => {
    const form = review({ port: "3002", webserver: "apache", ssl: false, layout: "inplace" });
    form.env = form.env.map((row) => ({ ...row, value: row.name === "LOG_LEVEL" ? "debug" : `${row.name.toLowerCase()}-value` }));
    expect(createAppBody({ source: " https://github.com/you/app.git ", branch: " main " }, { ...form, domain: " Shop.Example.com " })).toEqual({
      domain: "shop.example.com",
      source: "https://github.com/you/app.git",
      branch: "main",
      app_type: "nextjs",
      port: 3002,
      webserver: "apache",
      ssl: false,
      layout: "inplace",
      include_www: false,
      memory_max_mb: null,
      cpu_quota_percent: null,
      tasks_max: null,
      env_vars: { DATABASE_URL: "database_url-value", NEXTAUTH_SECRET: "nextauth_secret-value", LOG_LEVEL: "debug" },
      skip_database: false,
    });
  });

  it("sends no branch for a directory and no port for a static site", () => {
    const body = createAppBody({ source: "/srv/landing", branch: "main" }, review({ appType: "static" }));
    expect(body).not.toHaveProperty("branch");
    expect(body).not.toHaveProperty("port");
  });

  it("sends include_www only where it would mean something, even if it was ticked before the domain changed", () => {
    const subdomain = createAppBody({ source: "/srv/app", branch: "" }, review({ domain: "shop.example.com", includeWww: true }));
    expect(subdomain.include_www).toBe(false);
    const bare = createAppBody({ source: "/srv/app", branch: "" }, review({ domain: "example.com", includeWww: true }));
    expect(bare.include_www).toBe(true);
  });

  it("sends persistent paths only on the releases layout, trimmed and without empty rows", () => {
    const releases = createAppBody(
      { source: "/srv/app", branch: "" },
      review({ layout: "releases", persistentPaths: [path("path:1", " storage "), path("path:2", "")] }),
    );
    expect(releases.persistent_paths).toEqual(["storage"]);

    const inplace = createAppBody({ source: "/srv/app", branch: "" }, review({ layout: "inplace", persistentPaths: [path("path:1", "storage")] }));
    expect(inplace).not.toHaveProperty("persistent_paths");
  });

  it("sends resource limits as numbers, or null for an empty field", () => {
    const body = createAppBody({ source: "/srv/app", branch: "" }, review({ limits: { memory: "512", cpu: "", tasks: "256" } }));
    expect(body).toMatchObject({ memory_max_mb: 512, cpu_quota_percent: null, tasks_max: 256 });
  });
});

describe("refusals", () => {
  it("sends field errors to the step that asks for the field", () => {
    expect(refusalOf(new ApiError(422, "validation_error", "Validation failed", null, { source: "field required" }))).toEqual({
      step: "source",
      fields: { source: "field required" },
    });
    expect(refusalOf(new ApiError(422, "validation_error", "Validation failed", null, { port: "not an integer", app_type: "bad" }))).toEqual({
      step: "review",
      fields: { port: "not an integer", appType: "bad" },
    });
  });

  it("puts a taken domain, a refused port and an unfetchable source on their fields", () => {
    expect(refusalOf(new ApiError(409, "conflict", "Application already exists: a.com"))).toEqual({ step: "review", fields: { domain: "Application already exists: a.com" } });
    expect(refusalOf(new ApiError(400, "porterror", "Port 3000 is already in use"))).toEqual({ step: "review", fields: { port: "Port 3000 is already in use" } });
    expect(refusalOf(new ApiError(400, "sourceerror", "Source path does not exist: /x"))).toEqual({ step: "source", fields: { source: "Source path does not exist: /x" } });
    expect(refusalOf(new ApiError(500, "internal", "boom"))).toBeNull();
    expect(refusalOf(new Error("boom"))).toBeNull();
  });
});

describe("generated secrets", () => {
  it("are 32 random bytes as URL-safe base64, from the source they are given", () => {
    const secret = generateSecret(32, (array) => array.fill(0xff));
    expect(secret).toBe("_".repeat(42) + "8");
    expect(generateSecret()).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(generateSecret()).not.toBe(generateSecret());
  });
});

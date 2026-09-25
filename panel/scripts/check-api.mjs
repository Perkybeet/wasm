// Fails when src/api/schema.gen.ts is not what openapi-typescript produces from openapi.json.
//
// openapi.json is exported from the FastAPI app by `python scripts/export_openapi.py`; the
// generated types are committed so that the console cannot call an endpoint that does not
// exist or read a field the backend does not send. CI runs this to catch a stale copy.

import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schema = path.join(root, "openapi.json");
const committed = path.join(root, "src/api/schema.gen.ts");

if (!existsSync(schema)) {
  console.error("panel/openapi.json is missing. Export it with: python scripts/export_openapi.py");
  process.exit(1);
}
if (!existsSync(committed)) {
  console.error("src/api/schema.gen.ts is missing. Generate it with: npm run gen:api");
  process.exit(1);
}

const dir = mkdtempSync(path.join(tmpdir(), "wasm-api-"));
try {
  const fresh = path.join(dir, "schema.gen.ts");
  const bin = path.join(root, "node_modules/.bin/openapi-typescript");
  const result = spawnSync(bin, [schema, "-o", fresh], { cwd: root, encoding: "utf8", timeout: 60_000 });
  if (result.status !== 0) {
    console.error(result.stderr || result.stdout || "openapi-typescript failed");
    process.exit(1);
  }
  if (readFileSync(fresh, "utf8") !== readFileSync(committed, "utf8")) {
    console.error("src/api/schema.gen.ts is stale. Regenerate it with: npm run gen:api");
    process.exit(1);
  }
  console.log("src/api/schema.gen.ts matches openapi.json");
} finally {
  rmSync(dir, { recursive: true, force: true });
}

import { describe, expect, it } from "vitest";

import {
  hasExportPrefixes,
  nameProblem,
  parseDotenv,
  pythonLines,
  pythonStrip,
  readBack,
  removeExportPrefixes,
  valueProblem,
} from "./dotenv";

// The fixtures the Python suite (tests/test_env_fixtures.py) parses with EnvManager, read with
// Node's own fs (they sit outside the panel, where Vite's module loader may not reach) and
// decoded as UTF-8 with every byte kept: the CRLF and BOM cases are the point. The app's
// tsconfig has no Node types, so the little of fs used here is typed locally.
interface NodeFs {
  readdirSync: (path: string) => string[];
  readFileSync: (path: string, encoding: "utf8") => string;
}
const NODE_FS = "node:fs";
const fs = (await import(/* @vite-ignore */ NODE_FS)) as NodeFs;
// Vitest runs from panel/; the fixtures are the repository's.
const { process } = globalThis as unknown as { process: { cwd: () => string } };
const DIR = `${process.cwd()}/../tests/fixtures/env/`;

const CASES = fs
  .readdirSync(DIR)
  .filter((file) => file.endsWith(".env"))
  .sort()
  .map((file) => {
    const name = file.replace(/\.env$/, "");
    const text = fs.readFileSync(DIR + file, "utf8");
    const expected = JSON.parse(fs.readFileSync(`${DIR}${name}.json`, "utf8")) as Record<string, string>;
    return { name, text, expected };
  });

describe("the shared .env fixtures", () => {
  it("are all found, each with its expected map", () => {
    expect(CASES.length).toBeGreaterThanOrEqual(9);
    for (const { name, expected } of CASES) expect(expected, `${name}.json`).toBeDefined();
  });

  it("keep their CRLF bytes", () => {
    const crlf = CASES.find((c) => c.name === "crlf");
    expect(crlf?.text).toContain("\r\n");
  });

  it.each(CASES)("parse $name exactly as EnvManager does", ({ text, expected }) => {
    expect(Object.fromEntries(parseDotenv(text).variables)).toEqual(expected);
  });
});

describe("parseDotenv", () => {
  it("keeps the first position of a name and the last value", () => {
    const { variables, assignments } = parseDotenv("A=1\nB=2\nA=3\n");
    expect([...variables]).toEqual([
      ["A", "3"],
      ["B", "2"],
    ]);
    expect(assignments.map((a) => a.line)).toEqual([1, 2, 3]);
  });

  it("reports lines that are neither comments nor assignments", () => {
    expect(parseDotenv("# ok\nnot an assignment\nA=1").skipped).toEqual([{ line: 2, name: "not an assignment", value: "" }]);
  });

  it("does not strip export, as WASM does not", () => {
    expect([...parseDotenv("export FOO=bar").variables.keys()]).toEqual(["export FOO"]);
  });
});

describe("Python's string rules", () => {
  it("strips what str.strip() strips, not what trim() does", () => {
    expect(pythonStrip("\x1f a \x85")).toBe("a");
    expect(pythonStrip("\ufeffa")).toBe("\ufeffa");
  });

  it("splits lines like str.splitlines() after universal newlines", () => {
    expect(pythonLines("a\r\nb\rc\x0bd\u2028e\n")).toEqual(["a", "b", "c", "d", "e"]);
    expect(pythonLines("")).toEqual([]);
    expect(pythonLines("a\n\n")).toEqual(["a", ""]);
  });
});

describe("what the API accepts", () => {
  it("names a bad name and says how to fix it", () => {
    expect(nameProblem("API_URL")).toBeNull();
    expect(nameProblem("export FOO")).toMatch(/does not strip "export"/);
    expect(nameProblem("")).toMatch(/no name/);
    expect(nameProblem("2FA")).toMatch(/start with a letter/);
  });

  it("refuses control characters in values, naming them", () => {
    expect(valueProblem("caf\xe9")).toBeNull();
    expect(valueProblem("a\tb")).toMatch(/a tab/);
    expect(valueProblem("a\x7fb")).toMatch(/U\+007F/);
  });
});

describe("export prefixes", () => {
  it("are found only where they start an assignment", () => {
    expect(hasExportPrefixes("export A=1")).toBe(true);
    expect(hasExportPrefixes("exported=1\n# export A=1\nEXPORT_DIR=/x")).toBe(false);
  });

  it("are removed without touching anything else", () => {
    expect(removeExportPrefixes("export A=1\n  export\tB='x'\nexported=1\nC=export D=2")).toBe(
      "A=1\n  B='x'\nexported=1\nC=export D=2",
    );
  });
});

describe("readBack", () => {
  it("is the value when the unquoted writer can keep it", () => {
    expect(readBack("postgres://u:p@h/db")).toBe("postgres://u:p@h/db");
    expect(readBack("hola # not a comment")).toBe("hola # not a comment");
  });

  it("shows what a value with surrounding spaces or quotes becomes", () => {
    expect(readBack("  padded  ")).toBe("padded");
    expect(readBack("'quoted'")).toBe("quoted");
  });
});

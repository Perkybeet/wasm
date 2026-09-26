/**
 * The end-to-end harness: the real backend, a browser, and the gates every page must pass.
 *
 * Each Playwright worker starts its own `scripts/console_server.py` - the real FastAPI app
 * over a seeded, sandboxed machine - and points `baseURL` at it, so tests in different
 * workers never share sessions, lockouts or state. Every test is watched for Content
 * Security Policy violations, console errors and uncaught exceptions, and fails on any of
 * them: a policy is only enforced in a browser, and a blocked script or style fails silently
 * in production. `expectNoA11yViolations` runs axe with the WCAG 2.2 AA rules.
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, test as base } from "@playwright/test";
import type { Page } from "@playwright/test";
import { spawn } from "node:child_process";
import { createHmac } from "node:crypto";
import { existsSync } from "node:fs";
import path from "node:path";
import { createInterface } from "node:readline";

const PANEL = path.resolve(import.meta.dirname, "..");
const REPO = path.resolve(PANEL, "..");
const SERVER_SCRIPT = path.join(REPO, "scripts", "console_server.py");

/** Seconds a console server has to print its address before the harness gives up. */
const START_TIMEOUT_MS = 60_000;

export interface ConsoleServer {
  url: string;
  token: string;
  /** Set when the server was started with two-factor sign-in enabled. */
  totpSecret: string | null;
  /**
   * A second factor nothing has spent yet: one of the server's backup codes. A TOTP code is
   * good once per 30-second step and purpose (RFC 6238, 5.2, which the server enforces), and a
   * suite that signs in every few seconds cannot wait for the next step, so every sign-in and
   * confirmation that is not about TOTP itself spends a backup code instead.
   */
  secondFactor: () => string;
}

export interface RunningServer extends ConsoleServer {
  stop: () => Promise<void>;
}

/** The Python that runs the backend: $WASM_PYTHON, the repository's venv, or python3. */
function python(): string {
  const configured = process.env.WASM_PYTHON;
  if (configured) return configured;
  const venv = path.join(REPO, ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python3";
}

/**
 * Starts `scripts/console_server.py` and waits for the JSON line it prints once it accepts
 * connections.
 */
export async function startConsoleServer(args: readonly string[] = []): Promise<RunningServer> {
  // WASM_E2E_STATIC points the backend at a private build (see console_server.py
  // --static-dir), so parallel work on the console never serves another's chunks.
  const staticDir = process.env.WASM_E2E_STATIC;
  const extra = staticDir ? ["--static-dir", staticDir] : [];
  const child = spawn(python(), [SERVER_SCRIPT, ...extra, ...args], {
    cwd: REPO,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    // Kept for the failure message only; bounded, the server runs for a whole worker.
    stderr = (stderr + chunk).slice(-8_000);
  });

  const exited = new Promise<number | null>((resolve) => {
    child.once("exit", (code) => {
      resolve(code);
    });
  });

  const lines = createInterface({ input: child.stdout });
  const first = new Promise<string>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`console_server.py printed nothing in ${String(START_TIMEOUT_MS / 1000)}s:\n${stderr}`));
    }, START_TIMEOUT_MS);
    lines.once("line", (line) => {
      clearTimeout(timer);
      resolve(line);
    });
    void exited.then((code) => {
      clearTimeout(timer);
      reject(new Error(`console_server.py exited with ${String(code)} before it was ready:\n${stderr}`));
    });
  });

  let parsed: { url: string; token: string; totp_secret: string | null; backup_codes?: string[] };
  try {
    parsed = JSON.parse(await first) as typeof parsed;
  } catch (error: unknown) {
    child.kill("SIGKILL");
    throw error;
  }

  const unspent = [...(parsed.backup_codes ?? [])];
  return {
    url: parsed.url,
    token: parsed.token,
    totpSecret: parsed.totp_secret,
    secondFactor: () => {
      const code = unspent.shift();
      if (code !== undefined) return code;
      // A server started without a pool of backup codes: fine for the one or two factors a
      // single test spends, each purpose once per step.
      if (parsed.totp_secret === null) throw new Error("the console server has no second factor");
      return totpCode(parsed.totp_secret);
    },
    stop: async () => {
      if (child.exitCode !== null) return;
      child.kill("SIGTERM");
      const timeout = new Promise<"timeout">((resolve) => setTimeout(() => { resolve("timeout"); }, 10_000));
      if ((await Promise.race([exited, timeout])) === "timeout") child.kill("SIGKILL");
    },
  };
}

/** RFC 6238 code for a base32 secret: SHA-1, 30-second steps, six digits. */
export function totpCode(secret: string, now: number = Date.now()): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    const value = alphabet.indexOf(char);
    if (value === -1) continue;
    bits += value.toString(2).padStart(5, "0");
  }
  const key = Buffer.alloc(Math.floor(bits.length / 8));
  for (let i = 0; i < key.length; i += 1) key[i] = parseInt(bits.slice(i * 8, i * 8 + 8), 2);

  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(now / 1000 / 30)));
  const digest = createHmac("sha1", key).update(counter).digest();
  const offset = (digest[digest.length - 1] ?? 0) & 0x0f;
  const binary = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(binary % 1_000_000).padStart(6, "0");
}

/**
 * Signs in through the real sign-in page: the token, then the two-factor code when the
 * server asks for one. Ends on the page `next` names (the overview by default).
 */
export async function signIn(page: Page, server: ConsoleServer, next?: string): Promise<void> {
  if (!page.url().includes("/login")) {
    await page.goto(next === undefined ? "/login" : `/login?next=${encodeURIComponent(next)}`);
  }
  await page.getByLabel("Access token").fill(server.token);
  await page.getByRole("button", { name: "Sign in" }).click();
  if (server.totpSecret !== null) {
    const code = page.getByLabel("Two-factor code");
    await expect(code).toBeFocused();
    await code.fill(server.secondFactor());
    await page.getByRole("button", { name: "Verify" }).click();
  }
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}

/**
 * Answers "Confirm it's you": with an unspent second factor when the server has two-factor
 * sign-in on, with the access token otherwise.
 */
export async function confirmItsYou(page: Page, server: ConsoleServer): Promise<void> {
  const dialog = page.getByRole("dialog", { name: "Confirm it's you" });
  await expect(dialog).toBeVisible();
  if (server.totpSecret !== null) await dialog.getByLabel("Authentication code").fill(server.secondFactor());
  else await dialog.getByLabel("Access token").fill(server.token);
  await dialog.getByRole("button", { name: "Confirm" }).click();
  await expect(dialog).toBeHidden();
}

/**
 * The toasts on screen. A toast is announced by living in a live region, and the page's own
 * announcer may say a related sentence too, so tests look for a toast here, deliberately,
 * never with a bare getByText that would also find the words in a live region.
 */
export function toasts(page: Page) {
  return page.getByRole("region", { name: "Notifications" });
}

/** Tags of the rules axe runs: WCAG 2.0, 2.1 and 2.2 at levels A and AA. */
const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22a", "wcag22aa"];

/**
 * Runs axe on the page as it is and fails with a readable list when anything violates
 * WCAG 2.2 AA.
 */
export async function expectNoA11yViolations(page: Page, label?: string): Promise<void> {
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  const report = results.violations.map((violation) => {
    const nodes = violation.nodes
      .slice(0, 5)
      .map((node) => `    - ${node.target.join(" ")}\n      ${node.failureSummary?.replace(/\n/g, "\n      ") ?? ""}`)
      .join("\n");
    return `${violation.id} (${violation.impact ?? "unknown"}): ${violation.help}\n  ${violation.helpUrl}\n${nodes}`;
  });
  expect(report, `axe found WCAG 2.2 AA violations${label === undefined ? "" : ` on ${label}`}:\n${report.join("\n")}`).toEqual([]);
}

/**
 * Waits for every finite animation and transition to end, so axe never measures a colour
 * mid-fade (a toast leaving, a dialog arriving) and a screenshot never catches one. Spinners
 * and pulses run forever and are left alone.
 */
export async function stillness(page: Page): Promise<void> {
  await page.waitForFunction(() =>
    document.getAnimations().every((animation) => {
      const iterations = animation.effect?.getComputedTiming().iterations;
      return animation.playState !== "running" || iterations === Infinity;
    }),
  );
}

/** Waits until fonts are in and the page has stopped moving, for axe and screenshots. */
export async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await stillness(page);
}

export interface PageProblems {
  /** Violations of the Content Security Policy, as the browser reported them. */
  csp: string[];
  /** console.error output and uncaught exceptions. */
  errors: string[];
  /**
   * Console errors this test causes on purpose. Chromium logs every failed fetch as a
   * console error, and a test that makes the server refuse something on purpose (a wrong
   * token, an expired session) causes one. The pattern is matched against the message
   * followed by the URL it came from; anything else still fails the test.
   */
  expect: (pattern: RegExp) => void;
}

/**
 * Refusals every sign-in produces and nothing else may: the first step of a two-factor
 * sign-in is answered 401 `totp_required` by design (the token was right, a code is
 * missing), and Chromium logs that as a failed resource.
 */
const SIGN_IN_PROTOCOL = /status of 401 .* \/api\/auth\/login$/;

interface Fixtures {
  problems: PageProblems;
}

interface WorkerFixtures {
  consoleServer: ConsoleServer;
}

export const test = base.extend<Fixtures, WorkerFixtures>({
  consoleServer: [
    // eslint-disable-next-line no-empty-pattern -- Playwright requires the destructuring form
    async ({}, use) => {
      // Two-factor on: the hardened configuration, and the sign-in every test goes through.
      // Enough backup codes for every sign-in and confirmation one worker makes.
      const server = await startConsoleServer(["--totp", "--backup-codes", "500"]);
      try {
        await use(server);
      } finally {
        await server.stop();
      }
    },
    { scope: "worker", timeout: START_TIMEOUT_MS + 15_000 },
  ],

  baseURL: async ({ consoleServer }, use) => {
    await use(consoleServer.url);
  },

  problems: [
    async ({ context }, use) => {
      const problems: PageProblems = { csp: [], errors: [], expect: () => undefined };
      const expected: RegExp[] = [SIGN_IN_PROTOCOL];
      problems.expect = (pattern) => {
        expected.push(pattern);
      };

      // Reported from the page through a binding, so a violation in a document the test
      // already navigated away from is not lost with that document.
      await context.exposeBinding("__wasmReportViolation", (_source, report: string) => {
        problems.csp.push(report);
      });
      await context.addInitScript(() => {
        document.addEventListener("securitypolicyviolation", (event) => {
          const report = (window as unknown as { __wasmReportViolation?: (text: string) => void }).__wasmReportViolation;
          report?.(
            `${event.effectiveDirective} blocked ${event.blockedURI || "inline"} at ${event.sourceFile || "?"}:${String(event.lineNumber)}` +
              (event.sample ? ` (${event.sample})` : ""),
          );
        });
      });
      context.on("console", (message) => {
        if (message.type() !== "error") return;
        const location = message.location().url;
        const text = `${message.text()} ${new URL(location || "about:blank").pathname}`;
        if (!expected.some((pattern) => pattern.test(text))) problems.errors.push(`console.error: ${text}`);
      });
      context.on("weberror", (error) => {
        problems.errors.push(`uncaught: ${error.error().message}`);
      });

      await use(problems);

      expect(problems.csp, `Content Security Policy violations:\n${problems.csp.join("\n")}`).toEqual([]);
      expect(problems.errors, `Console errors:\n${problems.errors.join("\n")}`).toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };

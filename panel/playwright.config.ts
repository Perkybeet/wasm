import { defineConfig, devices } from "@playwright/test";

// The E2E suite runs against the real backend: every worker starts its own
// scripts/console_server.py (see e2e/fixtures.ts) and uses its address as baseURL, so there
// is no web server to configure here and no port to agree on.
//
// The screenshot pass (e2e/screenshots.spec.ts, tagged @screens) is slow and produces
// files for a person to review, so it is left out of the default run. `npm run e2e:screens`
// sets WASM_SCREENS=1, which lifts the exclusion; passing --grep alone would not, because
// Playwright applies grep and grepInvert together.
const screens = process.env.WASM_SCREENS === "1";

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  // Each worker runs a Python backend beside its browser.
  workers: process.env.CI ? 2 : 4,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  ...(screens ? { grep: /@screens/ } : { grepInvert: /@screens/ }),
  use: {
    trace: "retain-on-failure",
  },
  projects: [
    { name: "light", use: { ...devices["Desktop Chrome"], colorScheme: "light" } },
    { name: "dark", use: { ...devices["Desktop Chrome"], colorScheme: "dark" } },
  ],
});

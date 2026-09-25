import { defineConfig, devices } from "@playwright/test";

// The E2E harness (Task 2.6) starts the real backend through scripts/console_server.py and
// exports its URL as CONSOLE_URL. Until then the suite is present but has no specs.
const baseURL = process.env.CONSOLE_URL ?? "http://127.0.0.1:5199";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  grepInvert: /@screens/,
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "light", use: { ...devices["Desktop Chrome"], colorScheme: "light" } },
    { name: "dark", use: { ...devices["Desktop Chrome"], colorScheme: "dark" } },
  ],
});

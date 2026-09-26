import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../../test/axe";
import { renderConsole } from "../../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../../test/fakes";
import type { RecordedCall, RouteHandler } from "../../../test/fakes";

const DOMAIN = "shop.example.com";
const ENV_PATH = `/api/apps/${DOMAIN}/env`;

const MASKED = {
  NODE_ENV: "production",
  DATABASE_URL: "postgres://shop:***@127.0.0.1:5432/shop",
  SESSION_SECRET: "***",
};

const CLEAR = {
  NODE_ENV: "production",
  DATABASE_URL: "postgres://shop:hunter2@127.0.0.1:5432/shop",
  SESSION_SECRET: "s3cr3t-value",
};

function envRoute(masked: Record<string, string> = MASKED, clear: Record<string, string> = CLEAR): RouteHandler {
  return (call: RecordedCall) => {
    const unmasked = call.search.get("unmask") === "true";
    return json(200, { domain: DOMAIN, variables: unmasked ? clear : masked, unmasked });
  };
}

async function environmentTab(extra: Record<string, RouteHandler> = {}) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/certs": () => json(200, { certificates: [], total: 0 }),
    "GET /api/jobs/active": () => json(200, { jobs: [], total: 0, active: 0 }),
    [`GET ${ENV_PATH}`]: envRoute(),
    ...extra,
  });
  const harness = renderConsole(`/apps/${DOMAIN}/environment`);
  await screen.findByRole("table", { name: `Environment variables of ${DOMAIN}` });
  return { ...harness, backend };
}

function unmaskCalls(calls: RecordedCall[]): RecordedCall[] {
  return calls.filter((call) => call.method === "GET" && call.path === ENV_PATH && call.search.get("unmask") === "true");
}

describe("the environment tab", () => {
  it("masks every value until it is revealed, and reads secrets in clear only when asked", async () => {
    const { user, backend } = await environmentTab();
    const table = screen.getByRole("table", { name: `Environment variables of ${DOMAIN}` });
    expect(within(table).getAllByText("Hidden")).toHaveLength(3);
    expect(within(table).queryByText("production")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reveal the value of NODE_ENV" }));
    expect(await within(table).findByText("production")).toBeInTheDocument();
    expect(unmaskCalls(backend.calls)).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Reveal the value of SESSION_SECRET" }));
    expect(await within(table).findByText("s3cr3t-value")).toBeInTheDocument();
    expect(unmaskCalls(backend.calls)).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Hide the value of SESSION_SECRET" }));
    expect(within(table).queryByText("s3cr3t-value")).not.toBeInTheDocument();
  });

  it("asks to confirm it's you before a secret is shown, then shows it", async () => {
    let elevated = false;
    const { user, backend } = await environmentTab({
      [`GET ${ENV_PATH}`]: (call) =>
        call.search.get("unmask") === "true" && !elevated
          ? problem(403, "elevation_required", "Confirm it's you to continue")
          : envRoute()(call),
      "POST /api/auth/elevate": () => {
        elevated = true;
        return json(200, { elevated_until: "2026-09-25T20:10:00+00:00" });
      },
    });
    await user.click(screen.getByRole("button", { name: "Reveal the value of SESSION_SECRET" }));
    const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(confirm).getByLabelText("Authentication code"), "123456");
    await user.click(within(confirm).getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("s3cr3t-value")).toBeInTheDocument();
    expect(backend.callsTo("POST /api/auth/elevate")).toHaveLength(1);
  });

  it("stages a pasted file, reviews it over the real values, saves exactly that map and offers a restart", async () => {
    const { user, backend } = await environmentTab({
      [`PUT ${ENV_PATH}`]: () => json(200, { domain: DOMAIN, restart_required: true }),
      [`POST /api/apps/${DOMAIN}/restart`]: () => json(200, { success: true, message: "Application restarted", domain: DOMAIN }),
    });
    await user.click(screen.getByRole("button", { name: "Paste .env" }));
    const paste = await screen.findByRole("dialog", { name: "Paste a .env file" });
    await user.click(within(paste).getByLabelText(".env contents"));
    await user.paste('# comment\r\nNODE_ENV=staging\r\nPORT = "3000"\r\n\r\nnot an assignment\r\nPORT=8080\r\n');
    expect(within(paste).getByText("2 variables found")).toBeInTheDocument();
    expect(within(paste).getByText("PORT is set on lines 3 and 6; the later line wins.")).toBeInTheDocument();
    expect(within(paste).getByText("Line 5 has no = and is skipped, as WASM skips it.")).toBeInTheDocument();
    await user.click(within(paste).getByRole("radio", { name: "Replace all" }));
    expect(within(paste).getByText(/2 current variables are removed/)).toBeInTheDocument();
    await user.click(within(paste).getByRole("button", { name: "Stage 2 variables" }));

    expect(await screen.findByText("4 unsaved changes")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Review and save" }));
    const review = await screen.findByRole("dialog", { name: "Review changes" });
    expect(within(review).getByText("1 added, 1 changed, 2 removed. 0 variables stay as they are.")).toBeInTheDocument();
    // The removed secret is compared in clear, never as its placeholder.
    await user.click(within(review).getByRole("switch", { name: "Show values" }));
    expect(within(review).getByText("s3cr3t-value")).toBeInTheDocument();
    await user.click(within(review).getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(backend.callsTo(`PUT ${ENV_PATH}`)[0]?.body).toEqual({ variables: { NODE_ENV: "staging", PORT: "8080" } });
    });
    const saved = await screen.findByRole("dialog", { name: "Environment saved" });
    await user.click(within(saved).getByRole("button", { name: "Restart now" }));
    await waitFor(() => {
      expect(backend.callsTo(`POST /api/apps/${DOMAIN}/restart`)).toHaveLength(1);
    });
  });

  it("adds and removes rows as a draft, keeping the secrets it never touched", async () => {
    const { user, backend } = await environmentTab({
      [`PUT ${ENV_PATH}`]: () => json(200, { domain: DOMAIN, restart_required: true }),
    });
    await user.click(screen.getByRole("button", { name: "Add variable" }));
    const dialog = await screen.findByRole("dialog", { name: "Add a variable" });
    await user.type(within(dialog).getByLabelText("Name"), "API_URL");
    await user.type(within(dialog).getByLabelText("Value"), "https://api.example.com");
    await user.click(within(dialog).getByRole("button", { name: "Add variable" }));
    await user.click(await screen.findByRole("button", { name: "Remove NODE_ENV" }));
    expect(screen.getByText("2 unsaved changes")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Review and save" }));
    await user.click(within(await screen.findByRole("dialog", { name: "Review changes" })).getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(backend.callsTo(`PUT ${ENV_PATH}`)[0]?.body).toEqual({
        variables: {
          DATABASE_URL: "postgres://shop:hunter2@127.0.0.1:5432/shop",
          SESSION_SECRET: "s3cr3t-value",
          API_URL: "https://api.example.com",
        },
      });
    });
  });

  it("reads export prefixes as a shell would, and refuses a name the API refuses", async () => {
    const { user } = await environmentTab();
    await user.click(screen.getByRole("button", { name: "Paste .env" }));
    const paste = await screen.findByRole("dialog", { name: "Paste a .env file" });
    const textarea = within(paste).getByLabelText(".env contents");
    await user.click(textarea);
    await user.paste("export NODE_ENV=production\nPORT=3000\n");
    expect(within(paste).getByRole("button", { name: "Stage 2 variables" })).toBeEnabled();

    await user.clear(textarea);
    await user.paste("MY VAR=1\nPORT=3000\n");
    expect(within(paste).getByText(/Line 1: "MY VAR" is not a variable name/)).toBeInTheDocument();
    expect(within(paste).getByRole("button", { name: "Stage 2 variables" })).toBeDisabled();
  });

  it("shows the API's refusal verbatim and keeps the draft", async () => {
    const { user } = await environmentTab({
      [`PUT ${ENV_PATH}`]: () =>
        problem(422, "validation_error", "Environment variable 'NOTE' contains a tab"),
    });
    await user.click(screen.getByRole("button", { name: "Edit NODE_ENV" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit NODE_ENV" });
    const value = within(dialog).getByLabelText("Value");
    await user.clear(value);
    await user.type(value, "staging");
    await user.click(within(dialog).getByRole("button", { name: "Update variable" }));
    await user.click(screen.getByRole("button", { name: "Review and save" }));
    const review = await screen.findByRole("dialog", { name: "Review changes" });
    await user.click(within(review).getByRole("button", { name: "Save changes" }));
    expect(await within(review).findByText("Environment variable 'NOTE' contains a tab")).toBeInTheDocument();
    expect(within(review).getByText("The environment was not saved")).toBeInTheDocument();
  });

  it("invites a first variable when the file is empty", async () => {
    fakeBackend({
      ...signedInRoutes(),
      [`GET ${ENV_PATH}`]: envRoute({}, {}),
    });
    renderConsole(`/apps/${DOMAIN}/environment`);
    expect(await screen.findByRole("heading", { name: "No environment variables" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Paste .env" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add variable" })).toBeInTheDocument();
  });

  // axe over the whole tab takes seconds when the suite runs every file at once.
  it("has no accessibility violations", { timeout: 20_000 }, async () => {
    const { user } = await environmentTab();
    await user.click(screen.getByRole("button", { name: "Reveal the value of NODE_ENV" }));
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

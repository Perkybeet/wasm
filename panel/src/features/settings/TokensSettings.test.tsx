import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";

const NOW = Date.now() / 1000;

interface Token {
  id: number;
  name: string;
  scope: string;
  created_at: number;
  expires_at: number | null;
  last_used_at: number | null;
  revoked_at: number | null;
}

function tokensBackend(initial: Token[]) {
  const tokens = [...initial];
  let elevated = false;
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/auth/tokens": () => json(200, { tokens }),
    "POST /api/auth/elevate": () => {
      elevated = true;
      return json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() });
    },
    "POST /api/auth/tokens": (call) => {
      if (!elevated) return problem(403, "elevation_required", "Confirm it's you to continue.");
      const body = call.body as { name: string; scope: string; expires_hours: number | null };
      if (tokens.some((token) => token.name === body.name)) {
        return problem(400, "securityerror", `An API token named '${body.name}' already exists`, { hint: "Pick a new name." });
      }
      const created = {
        id: tokens.length + 10,
        name: body.name,
        scope: body.scope,
        created_at: NOW,
        expires_at: body.expires_hours === null ? null : NOW + body.expires_hours * 3600,
      };
      tokens.unshift({ ...created, last_used_at: null, revoked_at: null });
      return json(201, { ...created, token: "wasm_tok_s3cr3t-value" });
    },
  });
  for (const token of initial) {
    backend.on(`DELETE /api/auth/tokens/${String(token.id)}`, () => {
      token.revoked_at = NOW;
      const index = tokens.findIndex((t) => t.id === token.id);
      tokens[index] = { ...token };
      return json(200, { success: true, revoked: token.name });
    });
  }
  return backend;
}

async function expectToast(text: string): Promise<void> {
  await waitFor(() => {
    expect([...document.querySelectorAll(".toast")].some((toast) => toast.textContent.includes(text))).toBe(true);
  });
}

const SEEDED: Token[] = [
  { id: 1, name: "ci-deploy", scope: "deploy", created_at: NOW - 86_400, expires_at: NOW + 80 * 86_400, last_used_at: NOW - 300, revoked_at: null },
  { id: 2, name: "old-script", scope: "admin", created_at: NOW - 90 * 86_400, expires_at: null, last_used_at: null, revoked_at: NOW - 86_400 },
  { id: 3, name: "grafana", scope: "read", created_at: NOW - 40 * 86_400, expires_at: NOW - 86_400, last_used_at: null, revoked_at: null },
];

describe("Settings > API tokens", () => {
  it("lists the tokens with their scope and state, live ones first, and passes axe", { timeout: 20_000 }, async () => {
    tokensBackend(SEEDED);
    const { container } = renderConsole("/settings/tokens");
    await screen.findByText("ci-deploy");
    const table = screen.getByRole("region", { name: "API tokens" });
    const [live, expired, revoked, ...rest] = within(table).getAllByRole("row").slice(1);
    if (!live || !expired || !revoked) throw new Error("missing rows");
    expect([live, expired, revoked, ...rest].map((row) => row.querySelector("td span[translate=no]")?.textContent)).toEqual([
      "ci-deploy",
      "grafana",
      "old-script",
    ]);
    expect(within(live).getByText("Active")).toBeInTheDocument();
    expect(within(expired).getByText("Expired")).toBeInTheDocument();
    expect(within(revoked).getByText("Revoked")).toBeInTheDocument();
    // Only a live token can be revoked.
    expect(within(table).getAllByRole("button", { name: /^Revoke / })).toHaveLength(1);
    await expectNoAxeViolations(container);
  });

  it("offers to create the first token when there is none", async () => {
    tokensBackend([]);
    renderConsole("/settings/tokens");
    expect(await screen.findByRole("heading", { name: "No API tokens yet" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create token" })).toBeInTheDocument();
  });

  it("creates a read token after confirming it's you, and shows it once", { timeout: 20_000 }, async () => {
    const backend = tokensBackend(SEEDED);
    const { user } = renderConsole("/settings/tokens");
    await screen.findByText("ci-deploy");
    await user.click(screen.getByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Create an API token" });
    await user.type(within(dialog).getByLabelText(/^Name/), "ci-read");
    expect(within(dialog).getByRole("radio", { name: "Read" })).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: "Deploy" })).toHaveAccessibleDescription(/The one for CI/);
    await expectNoAxeViolations(dialog);
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));

    const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(confirm).getByLabelText("Authentication code"), "123456");
    await user.click(within(confirm).getByRole("button", { name: "Confirm" }));

    const once = await screen.findByRole("dialog", { name: "Copy your new token" });
    expect(within(once).getByTestId("new-token")).toHaveTextContent("wasm_tok_s3cr3t-value");
    expect(within(once).getByRole("alert")).toHaveTextContent("This is the only time the token is shown");
    expect(within(once).getByRole("button", { name: "Copy token" })).toBeInTheDocument();
    expect(backend.callsTo("POST /api/auth/tokens").at(-1)?.body).toEqual({ name: "ci-read", scope: "read", expires_hours: 90 * 24 });

    await user.click(within(once).getByRole("button", { name: "Done" }));
    await expectToast("Created token ci-read");
    expect(await screen.findByText("ci-read")).toBeInTheDocument();
    // Gone from the page: it is never shown again.
    expect(screen.queryByText("wasm_tok_s3cr3t-value")).not.toBeInTheDocument();
  });

  it("shows why a token could not be created, with the fix", { timeout: 20_000 }, async () => {
    tokensBackend(SEEDED);
    const { user } = renderConsole("/settings/tokens");
    await screen.findByText("ci-deploy");
    await user.click(screen.getByRole("button", { name: "Create token" }));
    const dialog = await screen.findByRole("dialog", { name: "Create an API token" });
    await user.type(within(dialog).getByLabelText(/^Name/), "ci-deploy");
    await user.click(within(dialog).getByRole("button", { name: "Create token" }));
    const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(confirm).getByLabelText("Authentication code"), "123456");
    await user.click(within(confirm).getByRole("button", { name: "Confirm" }));
    const block = await within(dialog).findByRole("alert");
    expect(block).toHaveTextContent("Pick a new name.");
    expect(block).toHaveTextContent("An API token named 'ci-deploy' already exists");
  });

  it("revokes a token once its name is typed", { timeout: 20_000 }, async () => {
    const backend = tokensBackend(SEEDED);
    const { user } = renderConsole("/settings/tokens");
    await screen.findByText("ci-deploy");
    await user.click(screen.getByRole("button", { name: "Revoke ci-deploy" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Revoke ci-deploy" });
    const action = within(dialog).getByRole("button", { name: "Revoke token" });
    expect(action).toBeDisabled();
    await user.type(within(dialog).getByRole("textbox"), "ci-deploy");
    await user.click(action);
    await expectToast("Revoked token ci-deploy");
    expect(backend.callsTo("DELETE /api/auth/tokens/1")).toHaveLength(1);
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Revoke ci-deploy" })).not.toBeInTheDocument();
    });
  });
});

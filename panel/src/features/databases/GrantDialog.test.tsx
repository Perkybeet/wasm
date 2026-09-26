import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

const ENGINES = {
  engines: [{ name: "postgresql", display_name: "PostgreSQL", installed: true, running: true, port: 5432, version: "16.4" }],
};

const DATABASES = {
  databases: [
    { name: "acme_shop", engine: "postgresql", size: "12 MB", tables: 4, owner: "wasm_app", encoding: "UTF8", connection_string: null },
  ],
  total: 1,
};

const USERS = {
  users: [{ username: "wasm_app", engine: "postgresql", host: "localhost", databases: ["acme_shop"], privileges: [] }],
  total: 1,
};

async function openGrantDialog(privileges: RouteHandler) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/databases/engines": () => json(200, ENGINES),
    "GET /api/databases/databases": () => json(200, DATABASES),
    "GET /api/databases/users/postgresql": () => json(200, USERS),
    "GET /api/databases/engines/postgresql/privileges": privileges,
  });
  const harness = renderConsole("/databases");
  await screen.findByRole("heading", { level: 1, name: "Databases" });
  const users = await screen.findByRole("region", { name: "Users" });
  const user = userEvent.setup();
  await user.click(await within(users).findByRole("button", { name: "Actions for wasm_app" }));
  await user.click(await screen.findByRole("menuitem", { name: "Grant privileges" }));
  const dialog = await screen.findByRole("dialog", { name: "Grant privileges to wasm_app" });
  return { ...harness, backend, dialog, user };
}

describe("the grant dialog's privilege list", () => {
  it("shows a loading state, then the engine's own privileges as checkboxes", async () => {
    let resolve: (response: Response) => void = () => undefined;
    const pending = new Promise<Response>((r) => {
      resolve = r;
    });
    const { dialog } = await openGrantDialog(() => pending);

    expect(within(dialog).getByText("Loading privileges")).toBeInTheDocument();
    resolve(json(200, { engine: "postgresql", privileges: ["ALL PRIVILEGES", "INSERT", "SELECT"] }));

    expect(await within(dialog).findByRole("checkbox", { name: "SELECT" })).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: "INSERT" })).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: "ALL PRIVILEGES" })).toBeInTheDocument();
    await expectNoAxeViolations(dialog);
  });

  it("submits only the checked privileges, none meaning the engine's own default", async () => {
    const { dialog, backend, user } = await openGrantDialog(() =>
      json(200, { engine: "postgresql", privileges: ["ALL PRIVILEGES", "INSERT", "SELECT"] }),
    );
    await within(dialog).findByRole("checkbox", { name: "SELECT" });
    backend.on("POST /api/databases/users/grant", () => json(200, { success: true, message: "Granted" }));

    await user.click(within(dialog).getByRole("combobox", { name: "Database" }));
    await user.click(await screen.findByRole("option", { name: "acme_shop" }));
    await user.click(within(dialog).getByRole("checkbox", { name: "SELECT" }));
    await user.click(within(dialog).getByRole("button", { name: "Grant" }));

    await waitFor(() => {
      expect(backend.callsTo("POST /api/databases/users/grant")).toHaveLength(1);
    });
    expect(backend.callsTo("POST /api/databases/users/grant")[0]?.body).toEqual({
      engine: "postgresql",
      username: "wasm_app",
      database: "acme_shop",
      host: "localhost",
      privileges: ["SELECT"],
    });
  });

  it("shows the privilege list's own load failure verbatim, without blocking the rest of the form", async () => {
    const { dialog } = await openGrantDialog(() => problem(500, "internal_error", "connection to the database refused"));

    await within(dialog).findByText("Could not load privileges");
    expect(within(dialog).getByText("connection to the database refused")).toBeInTheDocument();
    expect(within(dialog).getByRole("combobox", { name: "Database" })).toBeInTheDocument();
    await expectNoAxeViolations(dialog);
  });
});

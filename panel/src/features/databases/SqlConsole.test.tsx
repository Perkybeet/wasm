import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";

const DATABASE = {
  name: "acme_shop",
  engine: "postgresql",
  size: "12 MB",
  tables: 4,
  owner: "wasm_app",
  encoding: "UTF8",
  connection_string: null,
};

function databasePage() {
  return fakeBackend({
    ...signedInRoutes(),
    "GET /api/databases/databases/postgresql/acme_shop": () => json(200, DATABASE),
    "GET /api/databases/backups": () => json(200, { backups: [], total: 0 }),
  });
}

describe("SqlConsole", () => {
  it("shows a failed statement's own output verbatim, apart from the one-line detail", async () => {
    databasePage().on("POST /api/databases/query", () =>
      problem(400, "query_failed", 'ERROR: syntax error at or near "SELCT"', {
        output: 'psql:query.sql:1: ERROR:  syntax error at or near "SELCT"\nLINE 1: SELCT * FROM apps;\n        ^',
      }),
    );
    const { user, container } = renderConsole("/databases/postgresql/acme_shop");
    await screen.findByRole("heading", { level: 1, name: "acme_shop" });
    const sql = screen.getByRole("region", { name: "SQL console" });
    const editor = sql.querySelector("textarea");
    if (!editor) throw new Error("No SQL editor");
    await user.type(editor, "SELCT * FROM apps;");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText('ERROR: syntax error at or near "SELCT"')).toBeInTheDocument();
    expect(screen.getByText(/LINE 1: SELCT \* FROM apps;/)).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });
});

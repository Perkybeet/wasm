import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../test/fakes";
import type { RouteHandler } from "../../test/fakes";

/** One backup, with every field `BackupInfo` requires, so a test only names what it varies. */
function backup(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    backup_id: "picconia-com_20260101_000000",
    domain: "picconia.com",
    timestamp: "2026-01-01T00:00:00+00:00",
    size: 1_048_576,
    size_human: "1 MB",
    age: "a day ago",
    description: "Nightly backup",
    includes_env: true,
    includes_node_modules: false,
    includes_build: false,
    has_database: false,
    database_backups: [],
    tags: [],
    last_verified_at: null,
    verified_ok: null,
    ...overrides,
  };
}

function backupsRoutes(backups: Record<string, unknown>[], extra: Record<string, RouteHandler> = {}): Record<string, RouteHandler> {
  return {
    ...signedInRoutes(),
    "GET /api/backups": () => json(200, { backups, total: backups.length }),
    "GET /api/backups/storage": () => json(200, { path: "/var/backups/wasm", total_size: 0, total_size_human: "0 B", backup_count: 0, domains: [] }),
    "GET /api/backup-schedules": () => json(200, { schedules: [], total: 0 }),
    ...extra,
  };
}

/** The table row naming `domain`. Async: the list loads from the fake backend after mount. */
async function row(name: string): Promise<HTMLElement> {
  const link = await screen.findByRole("link", { name });
  const found = link.closest("tr");
  if (!found) throw new Error(`no row for ${name}`);
  return found;
}

describe("BackupsTable", () => {
  it("shows each backup's own last verification: never checked, verified, and failed", { timeout: 20_000 }, async () => {
    fakeBackend(
      backupsRoutes([
        backup({ backup_id: "b-never", domain: "never.example.com" }),
        backup({
          backup_id: "b-ok",
          domain: "ok.example.com",
          verified_ok: true,
          last_verified_at: new Date().toISOString(),
        }),
        backup({
          backup_id: "b-bad",
          domain: "bad.example.com",
          verified_ok: false,
          last_verified_at: new Date().toISOString(),
        }),
      ]),
    );
    const { container } = renderConsole("/backups");
    await screen.findByRole("heading", { level: 1, name: "Backups" });

    expect(within(await row("never.example.com")).getByText("Never verified")).toBeInTheDocument();
    expect(within(await row("ok.example.com")).getByText("Verified")).toBeInTheDocument();
    expect(within(await row("bad.example.com")).getByText("Verification failed")).toBeInTheDocument();
    await expectNoAxeViolations(container);
  });

  it("verifying a backup shows the server's fresh verdict, not a session-only guess", async () => {
    let verified: { verified_ok: boolean | null; last_verified_at: string | null } = { verified_ok: null, last_verified_at: null };
    const backend = fakeBackend(
      backupsRoutes([backup({ backup_id: "b-1", domain: "shop.example.com" })], {
        "GET /api/backups": () =>
          json(200, { backups: [backup({ backup_id: "b-1", domain: "shop.example.com", ...verified })], total: 1 }),
        "POST /api/backups/b-1/verify": () => {
          verified = { verified_ok: true, last_verified_at: new Date().toISOString() };
          return json(200, { backup_id: "b-1", valid: true, checksum_ok: true, files_ok: true, errors: [], warnings: [] });
        },
      }),
    );
    const { user } = renderConsole("/backups");
    await screen.findByRole("heading", { level: 1, name: "Backups" });
    expect(within(await row("shop.example.com")).getByText("Never verified")).toBeInTheDocument();

    await user.click(within(await row("shop.example.com")).getByRole("button", { name: /^Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Verify" }));

    await waitFor(async () => {
      expect(within(await row("shop.example.com")).getByText("Verified")).toBeInTheDocument();
    });
    expect(backend.callsTo("POST /api/backups/b-1/verify")).toHaveLength(1);
    // The list was refetched: the row reflects what GET /api/backups now answers, not a local flag.
    expect(backend.callsTo("GET /api/backups").length).toBeGreaterThan(1);
  });

  it("reports a failed verification with the server's own detail", async () => {
    fakeBackend(
      backupsRoutes([backup({ backup_id: "b-2", domain: "shop.example.com" })], {
        "POST /api/backups/b-2/verify": () =>
          json(200, {
            backup_id: "b-2",
            valid: false,
            checksum_ok: false,
            files_ok: true,
            errors: ["Checksum mismatch: the archive changed since it was created"],
            warnings: [],
          }),
      }),
    );
    const { user } = renderConsole("/backups");
    await screen.findByRole("heading", { level: 1, name: "Backups" });
    await user.click(within(await row("shop.example.com")).getByRole("button", { name: /^Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Verify" }));

    await waitFor(() => {
      expect(
        [...document.querySelectorAll(".toast")].some((toast) =>
          toast.textContent.includes("Checksum mismatch: the archive changed since it was created"),
        ),
      ).toBe(true);
    });
  });

  it("asks to confirm it's you before deleting a backup", async () => {
    let elevated = false;
    fakeBackend(
      backupsRoutes([backup({ backup_id: "b-3", domain: "shop.example.com" })], {
        "DELETE /api/backups/b-3": () =>
          elevated
            ? json(200, { success: true, message: "Backup deleted: b-3", backup_id: "b-3" })
            : problem(403, "elevation_required", "Confirm it's you to continue."),
        "POST /api/auth/elevate": () => {
          elevated = true;
          return json(200, { elevated_until: new Date(Date.now() + 600_000).toISOString() });
        },
      }),
    );
    const { user } = renderConsole("/backups");
    await screen.findByRole("heading", { level: 1, name: "Backups" });

    await user.click(within(await row("shop.example.com")).getByRole("button", { name: /^Actions for/ }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Delete b-3" });
    await user.type(within(dialog).getByRole("textbox"), "b-3");
    await user.click(within(dialog).getByRole("button", { name: "Delete backup" }));

    const confirm = await screen.findByRole("dialog", { name: "Confirm it's you" });
    await user.type(within(confirm).getByLabelText("Authentication code"), "123456");
    await user.click(within(confirm).getByRole("button", { name: "Confirm" }));

    await waitFor(() => {
      expect([...document.querySelectorAll(".toast")].some((toast) => toast.textContent.includes("Backup deleted: b-3"))).toBe(true);
    });
  });
});

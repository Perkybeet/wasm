import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { BackupStorage } from "../../api/queries/backups";
import { expectNoAxeViolations } from "../../test/axe";
import { renderConsole } from "../../test/console";
import { fakeBackend, json, signedInRoutes } from "../../test/fakes";
import { backupFilesystem, backupSummary } from "./StorageUsageBar";

const GB = 1024 ** 3;

function storage(overrides: Partial<BackupStorage> = {}): BackupStorage {
  return {
    path: "/mnt/backups/wasm",
    total_size: 3 * GB,
    total_size_human: "3.00 GB",
    backup_count: 12,
    domains: ["shop-example-com", "api-example-com"],
    misplaced: [],
    filesystem_total: 500 * GB,
    filesystem_free: 120 * GB,
    ...overrides,
  };
}

function backupsPage(answer: BackupStorage) {
  fakeBackend({
    ...signedInRoutes(),
    "GET /api/backups": () => json(200, { backups: [], total: 0 }),
    "GET /api/backups/storage": () => json(200, answer),
    "GET /api/backup-schedules": () => json(200, { schedules: [], total: 0 }),
  });
  return renderConsole("/backups");
}

describe("backup storage", () => {
  it("reads the filesystem the backup directory is on, and nothing when it could not be read", () => {
    expect(backupFilesystem(storage())).toEqual({ total: 500 * GB, free: 120 * GB, used: 380 * GB });
    expect(backupFilesystem(storage({ filesystem_total: null, filesystem_free: null }))).toBeNull();
    expect(backupSummary(storage())).toBe("3.00 GB in 12 backups of 2 applications, kept at ");
    expect(backupSummary(storage({ backup_count: 1, domains: ["a"] }))).toBe("3.00 GB in 1 backup of 1 application, kept at ");
  });

  it("measures the backup directory's own disk, used and free, and names the directory", async () => {
    const { container } = backupsPage(storage());
    const meter = await screen.findByRole("meter", { name: "Disk holding the backups" });
    expect(meter).toHaveAttribute("aria-valuetext", "380 GB used, 120 GB free of 500 GB");
    expect(screen.getByText("/mnt/backups/wasm")).toBeInTheDocument();
    expect(screen.getByText(/^3\.00 GB in 12 backups of 2 applications, kept at/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /outside the backup directory/ })).toBeNull();
    await expectNoAxeViolations(container);
  });

  it("says so when the disk could not be read, instead of drawing an empty meter", async () => {
    backupsPage(storage({ filesystem_total: null, filesystem_free: null }));
    expect(await screen.findByText("Its size and free space could not be read")).toBeInTheDocument();
    expect(screen.queryByRole("meter", { name: "Disk holding the backups" })).toBeNull();
  });

  it("names backups found outside the backup directory, with the exact command that imports them", async () => {
    const { container } = backupsPage(
      storage({
        misplaced: [
          { directory: "/root", count: 3, command: "wasm backup import /root" },
          { directory: "/var/backups/wasm", count: 1, command: "wasm backup import /var/backups/wasm" },
        ],
      }),
    );
    const notice = await screen.findByRole("region", { name: "4 backups are outside the backup directory" });
    expect(notice).toHaveTextContent("3 backups in /root");
    expect(notice).toHaveTextContent("1 backup in /var/backups/wasm");
    expect(notice).toHaveTextContent("wasm backup import /root");
    expect(notice).toHaveTextContent("wasm backup import /var/backups/wasm");
    expect(notice).toHaveTextContent("Add --dry-run to the command to see what would move first, without moving anything.");
    await expectNoAxeViolations(container);
  });
});

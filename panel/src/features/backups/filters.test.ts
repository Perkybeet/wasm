import { describe, expect, it } from "vitest";

import type { BackupList } from "../../api/queries/backups";
import { backupDomains, filterBackups, isFiltered, validateBackupsSearch } from "./filters";

function backup(overrides: Partial<BackupList["backups"][number]> = {}): BackupList["backups"][number] {
  return {
    backup_id: "shop_20260101_000000",
    domain: "shop.example.com",
    timestamp: "2026-01-01T00:00:00",
    size: 1024,
    size_human: "1.00 KB",
    age: "1 day",
    description: "",
    includes_env: false,
    includes_node_modules: false,
    includes_build: false,
    has_database: false,
    database_backups: [],
    tags: [],
    ...overrides,
  };
}

describe("validateBackupsSearch", () => {
  it("keeps a domain and the database flag", () => {
    expect(validateBackupsSearch({ domain: "shop.example.com", database: "1" })).toEqual({
      domain: "shop.example.com",
      database: true,
    });
  });

  it("drops malformed input instead of failing", () => {
    expect(validateBackupsSearch({ domain: 42, database: "yes" })).toEqual({});
    expect(validateBackupsSearch({})).toEqual({});
  });
});

describe("isFiltered", () => {
  it("is false with nothing set", () => {
    expect(isFiltered({})).toBe(false);
  });

  it("is true with either filter set", () => {
    expect(isFiltered({ domain: "shop.example.com" })).toBe(true);
    expect(isFiltered({ database: true })).toBe(true);
  });
});

describe("filterBackups", () => {
  const backups = [backup({ backup_id: "a", has_database: true }), backup({ backup_id: "b", has_database: false })];

  it("keeps everything when the database filter is off", () => {
    expect(filterBackups(backups, {})).toHaveLength(2);
  });

  it("keeps only backups with a database dump when it is on", () => {
    const filtered = filterBackups(backups, { database: true });
    expect(filtered.map((b) => b.backup_id)).toEqual(["a"]);
  });
});

describe("backupDomains", () => {
  it("lists every domain once, alphabetically", () => {
    const backups = [backup({ domain: "z.example.com" }), backup({ domain: "a.example.com" }), backup({ domain: "z.example.com" })];
    expect(backupDomains(backups)).toEqual(["a.example.com", "z.example.com"]);
  });
});

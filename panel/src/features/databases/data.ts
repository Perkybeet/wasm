/**
 * Shared engine vocabulary: the four engines `wasm.managers.database.DatabaseRegistry`
 * knows, in the order the CLI lists them, and which of them enforce a read-only grammar for
 * the SQL console (`READ_MODE_ENGINES` in `databases.py`).
 */

export const ENGINES = ["postgresql", "mysql", "redis", "mongodb"] as const;

const DISPLAY_NAMES: Readonly<Record<string, string>> = {
  postgresql: "PostgreSQL",
  postgres: "PostgreSQL",
  mysql: "MySQL/MariaDB",
  mariadb: "MySQL/MariaDB",
  redis: "Redis",
  mongodb: "MongoDB",
};

/** The engine's display name, or the raw value for one the console does not special-case. */
export function engineLabel(engine: string): string {
  return DISPLAY_NAMES[engine.toLowerCase()] ?? engine;
}

/** Engines whose read-only grammar the server enforces; every other engine is write-only. */
export const READ_MODE_ENGINES = new Set(["postgresql", "postgres", "mysql", "mariadb"]);

export function supportsReadMode(engine: string): boolean {
  return READ_MODE_ENGINES.has(engine.toLowerCase());
}

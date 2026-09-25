/**
 * Turns a query's raw output into a grid the results table can render.
 *
 * `POST /api/databases/query` answers with one string (see `databases.py`'s module
 * docstring): PostgreSQL runs `psql -t -A` (tuples only, unaligned) and MySQL runs
 * `mysql -N -B` (no column names, tab-separated) precisely so the output is one row per
 * line with no header to strip. That is correct for a client that only needs the values, but
 * it means no endpoint anywhere reports what the columns are called - not even their count
 * until a row arrives. The console cannot show real column names it was never sent; it shows
 * a row number instead and says so once, next to the table, rather than inventing names.
 */

const DELIMITER: Readonly<Record<string, RegExp>> = {
  postgresql: /\|/,
  postgres: /\|/,
  mysql: /\t/,
  mariadb: /\t/,
};

export interface QueryGrid {
  /** One label per column; positional ("Column 1"), since the engines report no names. */
  columns: string[];
  rows: string[][];
}

/** The field separator a console query's output uses for this engine. */
export function fieldDelimiter(engine: string): RegExp {
  return DELIMITER[engine.toLowerCase()] ?? /\t/;
}

/**
 * Splits a query's output into rows and cells. Ragged rows (a NULL trailing column prints as
 * nothing, which some engines drop rather than print an empty field) are padded to the widest
 * row seen, so every row has a cell under every column.
 */
export function parseQueryGrid(engine: string, output: string): QueryGrid {
  const delimiter = fieldDelimiter(engine);
  const lines = output.split("\n").filter((line) => line !== "");
  const cells = lines.map((line) => line.split(delimiter));
  const width = cells.reduce((max, row) => Math.max(max, row.length), 0);
  if (width === 0) return { columns: [], rows: [] };
  const columns = Array.from({ length: width }, (_, index) => `Column ${String(index + 1)}`);
  const rows = cells.map((row) => Array.from({ length: width }, (_, index) => row[index] ?? ""));
  return { columns, rows };
}

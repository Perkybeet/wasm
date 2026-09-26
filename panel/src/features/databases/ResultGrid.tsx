import { useId } from "react";

import { SystemOutput } from "../../components/ui/SystemOutput";
import { formatCount, formatDuration } from "../../lib/format";

/**
 * What `POST /api/databases/query` answers, in the shape this feature renders (see
 * `databases.py`'s `QueryResponse` and `BaseDatabaseManager.execute_query_structured`): columns
 * and rows the engine's own client parsed, timed and counted server-side, plus its raw output
 * for the engines and statements that have no tabular shape to parse.
 */
export interface QueryResult {
  /** Column names, in the engine's own order. Empty when there is no result set to name. */
  columns: string[];
  /** Data rows, each cell exactly as the client printed it. */
  rows: string[][];
  /** Rows in `rows`, after truncation. */
  rowCount: number;
  /** Wall-clock time the client invocation took. */
  durationMs: number;
  /** Whether the result (structured or raw) was cut short. */
  truncated: boolean;
  /** The client's own output, verbatim - the only thing to show when there are no columns. */
  output: string;
}

/**
 * "12 rows in 4 ms": how many rows came back and how long the engine's own client took, in
 * tabular figures so re-running the same query does not jitter the line. A statement with no
 * result set - a write, a DDL, an engine with no tabular client output to parse - has nothing
 * to count, so it reports only the time.
 */
export function formatResultLine(result: Pick<QueryResult, "columns" | "rowCount" | "durationMs">): string {
  const duration = formatDuration(result.durationMs / 1000);
  if (result.columns.length === 0) return `Ran in ${duration}`;
  return `${formatCount(result.rowCount)} ${result.rowCount === 1 ? "row" : "rows"} in ${duration}`;
}

/**
 * A cell exactly as the client printed it, with SQL NULL and an empty string marked apart from
 * an ordinary value: both print as nothing otherwise, and an operator debugging data cannot
 * tell a missing value from one that is merely blank.
 */
function Cell({ value }: { value: string }) {
  if (value === "NULL") {
    return (
      <span aria-label="SQL NULL" className="text-fg-faint italic">
        NULL
      </span>
    );
  }
  if (value === "") {
    return (
      <span aria-label="Empty string" className="text-fg-faint italic">
        empty
      </span>
    );
  }
  return <>{value}</>;
}

/** Sticky-header grid for a structured result: columns and rows named and printed by the engine's own client. */
function Grid({ columns, rows }: { columns: string[]; rows: string[][] }) {
  const captionId = useId();
  return (
    <div
      role="region"
      aria-labelledby={captionId}
      tabIndex={0}
      className="relative max-h-[26rem] min-w-0 overflow-auto rounded-card border border-border bg-surface shadow-raised scroll-thin focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
    >
      <table className="w-full border-collapse text-left text-13">
        <caption id={captionId} className="sr-only">
          Query result
        </caption>
        <thead className="sticky top-0 z-10 bg-bg-sunken">
          <tr className="border-b border-border">
            {columns.map((column, index) => (
              <th
                key={`${column}-${String(index)}`}
                scope="col"
                className="mono h-9 px-3 text-left text-12 font-medium whitespace-nowrap text-fg-muted first:pl-4"
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={String(rowIndex)} className="border-b border-border last:border-0">
              {columns.map((_column, cellIndex) => (
                <td key={String(cellIndex)} className="mono h-9 px-3 text-12 whitespace-nowrap text-fg first:pl-4">
                  <Cell value={row[cellIndex] ?? ""} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * A statement's result: the grid when the engine's client reported columns to name, its raw
 * output verbatim otherwise (Redis, MongoDB, or a statement such as an UPDATE or a DDL with no
 * result set to name).
 */
export function ResultGrid({ result }: { result: QueryResult }) {
  const hasGrid = result.columns.length > 0;
  return (
    <div className="flex flex-col gap-2">
      <p role="status" className="mono text-12 text-fg-muted">
        {formatResultLine(result)}
      </p>
      {result.truncated ? (
        <p className="rounded-control border border-border bg-bg-sunken px-3 py-2 text-12 text-fg-muted">
          {hasGrid
            ? `Only the first ${formatCount(result.rowCount)} ${result.rowCount === 1 ? "row is" : "rows are"} shown; the statement returned more.`
            : "The output was cut short; the statement returned more."}
        </p>
      ) : null}
      {hasGrid ? (
        <Grid columns={result.columns} rows={result.rows} />
      ) : result.output.trim() === "" ? (
        <p className="text-13 text-fg-muted">The statement returned no rows.</p>
      ) : (
        <div className="rounded-control border border-border bg-bg-sunken px-3 py-2">
          <SystemOutput label="The statement's raw output" maxHeight="max-h-72">
            {result.output}
          </SystemOutput>
        </div>
      )}
    </div>
  );
}

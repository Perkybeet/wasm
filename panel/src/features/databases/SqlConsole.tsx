import { Play } from "lucide-react";
import { useState } from "react";
import type { KeyboardEvent } from "react";

import { ErrorBlock } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { SegmentedControl } from "../../components/page/SegmentedControl";
import { Button } from "../../components/ui/Button";
import { DataTable } from "../../components/ui/DataTable";
import type { Column } from "../../components/ui/DataTable";
import { Textarea } from "../../components/ui/Textarea";
import { formatDuration } from "../../lib/format";
import { engineLabel, supportsReadMode } from "./data";
import { parseQueryGrid } from "./grid";
import type { QueryGrid } from "./grid";
import { useDatabaseActions } from "./useDatabaseActions";

interface Row {
  index: number;
  cells: string[];
}

interface Result {
  grid: QueryGrid;
  durationMs: number;
  returnedLines: number;
  truncated: boolean;
}

/**
 * A mono editor that runs one statement at a time (databases.py's `/query` endpoint refuses an
 * embedded `;`), read-only by default where the engine's grammar supports it, write mode
 * always asking the API client's "Confirm it's you" dialog for a fresh elevation. The engine
 * reports no column names (see `grid.ts`) and no duration, so the console times the round trip
 * itself and labels columns by position.
 */
export function SqlConsole({ engine, database }: { engine: string; database: string }) {
  const readAllowed = supportsReadMode(engine);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<"read" | "write">(readAllowed ? "read" : "write");
  const [result, setResult] = useState<Result | null>(null);
  const { runQuery } = useDatabaseActions();

  const run = (): void => {
    const statement = query.trim();
    if (statement === "" || runQuery.isPending) return;
    const startedAt = performance.now();
    runQuery.mutate(
      { engine, database, query: statement, mode: readAllowed ? mode : "write" },
      {
        onSuccess: (response) => {
          setResult({
            grid: parseQueryGrid(engine, response.output),
            durationMs: performance.now() - startedAt,
            returnedLines: response.returned_rows,
            truncated: response.truncated,
          });
        },
      },
    );
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      run();
    }
  };

  const columns: Column<Row>[] =
    result?.grid.columns.map((label, index) => ({
      id: `c${String(index)}`,
      header: label,
      mono: true,
      cell: (row) => row.cells[index] ?? "",
    })) ?? [];

  const rows: Row[] = result?.grid.rows.map((cells, index) => ({ index, cells })) ?? [];

  return (
    <Section
      title="SQL console"
      description="One statement at a time. Read mode runs it as a least-privilege role, in a transaction that refuses writes; write mode runs it as this engine's own superuser."
    >
      <div className="flex flex-col gap-3 rounded-card border border-border bg-surface p-4 shadow-raised">
        <div className="flex flex-wrap items-center justify-between gap-3">
          {readAllowed ? (
            <SegmentedControl
              label="Mode"
              value={mode}
              onValueChange={setMode}
              options={[
                { value: "read", label: "Read" },
                { value: "write", label: "Write" },
              ]}
            />
          ) : (
            <p className="text-12 text-fg-faint">
              {engineLabel(engine)} has no read-only grammar WASM enforces here; every statement runs in write mode.
            </p>
          )}
        </div>
        <Textarea
          mono
          rows={6}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
          }}
          onKeyDown={onKeyDown}
          placeholder={engine === "redis" ? "GET session:abc123" : "SELECT * FROM ..."}
          spellCheck={false}
          autoCapitalize="off"
        />
        <div className="flex items-center justify-between gap-3">
          <p className="text-12 text-fg-faint">Ctrl+Enter runs.</p>
          <Button variant="primary" icon={<Play aria-hidden="true" />} loading={runQuery.isPending} disabled={query.trim() === ""} onClick={run}>
            Run
          </Button>
        </div>
        {runQuery.isError ? <ErrorBlock live error={runQuery.error} title="The statement failed" /> : null}
        {result !== null ? (
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-12">
              <p role="status" className="text-fg-muted">
                {`${String(result.grid.rows.length)} ${result.grid.rows.length === 1 ? "row" : "rows"} · ${formatDuration(result.durationMs / 1000)}`}
                {result.truncated ? ` · truncated to ${String(result.returnedLines)} lines` : ""}
              </p>
              {result.grid.rows.length > 0 ? (
                <p className="text-fg-faint">Columns are not named by the engine in this mode; shown by position.</p>
              ) : null}
            </div>
            {result.grid.rows.length > 0 ? (
              <DataTable columns={columns} rows={rows} getRowId={(row) => String(row.index)} caption="Query result" density="compact" />
            ) : (
              <p className="text-13 text-fg-muted">The statement returned no rows.</p>
            )}
          </div>
        ) : null}
      </div>
    </Section>
  );
}

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { formatResultLine, ResultGrid } from "./ResultGrid";
import type { QueryResult } from "./ResultGrid";

const TABULAR: QueryResult = {
  columns: ["id", "email", "total"],
  rows: [
    ["1024", "maria@example.com", "129.90"],
    ["1025", "jon@example.com", "54.00"],
  ],
  rowCount: 2,
  durationMs: 4,
  truncated: false,
  output: "id,email,total\n1024,maria@example.com,129.90\n1025,jon@example.com,54.00\n",
};

describe("formatResultLine", () => {
  it("counts rows and singularises one", () => {
    expect(formatResultLine({ columns: ["id"], rowCount: 12, durationMs: 4 })).toBe("12 rows in 4 ms");
    expect(formatResultLine({ columns: ["id"], rowCount: 1, durationMs: 4 })).toBe("1 row in 4 ms");
    expect(formatResultLine({ columns: ["id"], rowCount: 0, durationMs: 4 })).toBe("0 rows in 4 ms");
  });

  it("reports only the time for a statement with no result set to count", () => {
    expect(formatResultLine({ columns: [], rowCount: 0, durationMs: 1500 })).toBe("Ran in 1.5s");
  });
});

describe("ResultGrid", () => {
  it("renders a sticky-header grid from columns and rows, with a result line", () => {
    render(<ResultGrid result={TABULAR} />);
    expect(screen.getByRole("columnheader", { name: "email" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "maria@example.com" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "129.90" })).toBeInTheDocument();
    expect(screen.getByText("2 rows in 4 ms")).toBeInTheDocument();
  });

  it("marks the grid a keyboard-reachable region with an accessible name", () => {
    render(<ResultGrid result={TABULAR} />);
    const region = screen.getByRole("region", { name: "Query result" });
    expect(region).toHaveAttribute("tabIndex", "0");
  });

  it("distinguishes a SQL NULL from an empty string, and both from a real value", () => {
    const withNulls: QueryResult = {
      ...TABULAR,
      rows: [["1026", "NULL", ""]],
      rowCount: 1,
    };
    render(<ResultGrid result={withNulls} />);
    expect(screen.getByLabelText("SQL NULL")).toHaveTextContent("NULL");
    expect(screen.getByLabelText("Empty string")).toHaveTextContent("empty");
    expect(screen.getByRole("cell", { name: "1026" })).toBeInTheDocument();
  });

  it("shows a clear truncation notice when the result was cut short", () => {
    render(<ResultGrid result={{ ...TABULAR, truncated: true }} />);
    expect(screen.getByText(/Only the first 2 rows are shown; the statement returned more\./)).toBeInTheDocument();
  });

  it("shows the raw output verbatim in mono when there are no columns to name", () => {
    const raw: QueryResult = {
      columns: [],
      rows: [],
      rowCount: 0,
      durationMs: 2,
      truncated: false,
      output: "OK\r\n",
    };
    render(<ResultGrid result={raw} />);
    expect(screen.getByText("OK")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says a statement returned no rows instead of an empty raw block", () => {
    const empty: QueryResult = { columns: [], rows: [], rowCount: 0, durationMs: 2, truncated: false, output: "" };
    render(<ResultGrid result={empty} />);
    expect(screen.getByText("The statement returned no rows.")).toBeInTheDocument();
  });

  it("marks a truncated raw fallback apart from a truncated grid", () => {
    const raw: QueryResult = { columns: [], rows: [], rowCount: 0, durationMs: 2, truncated: true, output: "partial output" };
    render(<ResultGrid result={raw} />);
    expect(screen.getByText(/The output was cut short; the statement returned more\./)).toBeInTheDocument();
  });

  it("passes axe in both the grid and the raw-output shapes", async () => {
    const { container, rerender } = render(<ResultGrid result={TABULAR} />);
    await expectNoAxeViolations(container);
    rerender(<ResultGrid result={{ columns: [], rows: [], rowCount: 0, durationMs: 2, truncated: false, output: "OK" }} />);
    await expectNoAxeViolations(container);
  });
});

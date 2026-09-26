import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, MouseEvent, ReactNode } from "react";

import { cx } from "../../lib/cx";
import { Skeleton } from "./Skeleton";

export type SortDirection = "ascending" | "descending";

export interface SortState {
  column: string;
  direction: SortDirection;
}

export interface Column<T> {
  id: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** Makes the column sortable by this value. */
  sortValue?: (row: T) => string | number | null;
  align?: "start" | "end";
  /** System values (ports, sizes, times) in mono with tabular figures. */
  mono?: boolean;
  /** A width utility, such as `w-40`. Columns without one share the rest. */
  width?: string;
  /** Hide the column on narrow screens to keep rows legible. */
  hideBelow?: "sm" | "md" | "lg";
}

export interface DataTableProps<T> {
  columns: readonly Column<T>[];
  rows: readonly T[];
  getRowId: (row: T) => string;
  /** Names the table for assistive technology; rendered visually hidden. */
  caption: string;
  sort?: SortState | null;
  defaultSort?: SortState | null;
  onSortChange?: (sort: SortState) => void;
  /**
   * Opening a row. The first column becomes the row's primary control: a click anywhere on
   * the row, or Enter on it, activates. Arrow keys move between rows.
   */
  onRowActivate?: (row: T) => void;
  /** Per-row controls at the end of the row, typically a Menu. */
  rowActions?: (row: T) => ReactNode;
  loading?: boolean;
  /**
   * How many placeholder rows to draw while loading: the number of rows the caller expects
   * (a count it already knows, a page size), so the table does not grow or shrink under the
   * content below it when the rows arrive. Five when unknown; at least one.
   */
  skeletonRows?: number;
  /** Shown instead of rows when there are none, usually an EmptyState. */
  empty?: ReactNode;
  density?: "compact" | "comfortable";
  className?: string;
}

const HIDE: Record<NonNullable<Column<unknown>["hideBelow"]>, string> = {
  sm: "hidden sm:table-cell",
  md: "hidden md:table-cell",
  lg: "hidden lg:table-cell",
};

const INTERACTIVE = "a, button, input, select, textarea, [role='menuitem'], [role='checkbox'], [role='switch']";

function compare(a: string | number | null, b: string | number | null): number {
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/**
 * Rows of like things: apps, deployments, databases. Sorting is announced through aria-sort
 * on the column header; rows are reachable and activatable from the keyboard.
 */
export function DataTable<T>({
  columns,
  rows,
  getRowId,
  caption,
  sort,
  defaultSort = null,
  onSortChange,
  onRowActivate,
  rowActions,
  loading = false,
  skeletonRows = 5,
  empty,
  density = "comfortable",
  className,
}: DataTableProps<T>) {
  const captionId = useId();
  const [internalSort, setInternalSort] = useState<SortState | null>(defaultSort);
  const activeSort = sort !== undefined ? sort : internalSort;
  const [focusedRow, setFocusedRow] = useState<string | null>(null);
  const bodyRef = useRef<HTMLTableSectionElement>(null);

  const sorted = useMemo(() => {
    if (!activeSort) return rows;
    const column = columns.find((c) => c.id === activeSort.column);
    const value = column?.sortValue;
    if (!value) return rows;
    const factor = activeSort.direction === "ascending" ? 1 : -1;
    return [...rows].sort((a, b) => factor * compare(value(a), value(b)));
  }, [rows, columns, activeSort]);

  const toggleSort = (column: Column<T>): void => {
    const direction: SortDirection =
      activeSort?.column === column.id && activeSort.direction === "ascending" ? "descending" : "ascending";
    const next = { column: column.id, direction };
    if (sort === undefined) setInternalSort(next);
    onSortChange?.(next);
  };

  const primaryIds = sorted.map(getRowId);
  const tabbableRow = focusedRow !== null && primaryIds.includes(focusedRow) ? focusedRow : primaryIds[0];

  const moveFocus = (event: KeyboardEvent<HTMLTableSectionElement>): void => {
    const target = event.target as HTMLElement;
    if (!target.matches("[data-row-primary]")) return;
    const current = primaryIds.indexOf(target.dataset["rowId"] ?? "");
    const last = primaryIds.length - 1;
    const targets: Record<string, number> = {
      ArrowDown: Math.min(current + 1, last),
      ArrowUp: Math.max(current - 1, 0),
      Home: 0,
      End: last,
    };
    const next = targets[event.key];
    if (next === undefined) return;
    event.preventDefault();
    const id = primaryIds[next];
    if (id === undefined) return;
    setFocusedRow(id);
    bodyRef.current?.querySelector<HTMLElement>(`[data-row-primary][data-row-id="${CSS.escape(id)}"]`)?.focus();
  };

  const onRowClick = (event: MouseEvent<HTMLTableRowElement>, row: T): void => {
    if (!onRowActivate) return;
    // Controls inside the row, the primary button included, handle their own presses.
    if ((event.target as HTMLElement).closest(INTERACTIVE)) return;
    onRowActivate(row);
  };

  const cellHeight = density === "compact" ? "h-9" : "h-11";
  const hasInteractive = Boolean(onRowActivate) || Boolean(rowActions) || columns.some((c) => c.sortValue);

  return (
    <div
      role="region"
      aria-labelledby={captionId}
      {...(hasInteractive ? {} : { tabIndex: 0 })}
      // `relative`: the containing block of the visually hidden labels in cells. Without it they
      // are positioned against the page, escape this scroll box and widen the page on a phone.
      className={cx(
        "relative min-w-0 overflow-x-auto rounded-card border border-border bg-surface shadow-raised scroll-thin",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        className,
      )}
    >
      <table aria-busy={loading || undefined} className="w-full border-collapse text-left text-13">
        <caption id={captionId} className="sr-only">
          {caption}
        </caption>
        <thead>
          <tr className="border-b border-border bg-bg-sunken/70">
            {columns.map((column) => {
              const sortedHere = activeSort?.column === column.id ? activeSort.direction : undefined;
              return (
                <th
                  key={column.id}
                  scope="col"
                  {...(sortedHere ? { "aria-sort": sortedHere } : {})}
                  className={cx(
                    "h-9 px-3 text-12 font-medium whitespace-nowrap text-fg-muted first:pl-4",
                    column.align === "end" && "text-right",
                    column.width,
                    column.hideBelow && HIDE[column.hideBelow],
                  )}
                >
                  {column.sortValue ? (
                    <button
                      type="button"
                      onClick={() => toggleSort(column)}
                      className={cx(
                        "-mx-1.5 inline-flex h-7 cursor-pointer items-center gap-1 rounded-control px-1.5 hover:bg-surface-hover hover:text-fg",
                        "focus-visible:outline-2 focus-visible:outline-focus",
                        sortedHere && "text-fg",
                        column.align === "end" && "flex-row-reverse",
                      )}
                    >
                      {column.header}
                      {sortedHere === "ascending" ? (
                        <ArrowUp aria-hidden="true" className="size-3.5" />
                      ) : sortedHere === "descending" ? (
                        <ArrowDown aria-hidden="true" className="size-3.5" />
                      ) : (
                        <ChevronsUpDown aria-hidden="true" className="size-3.5 text-fg-faint" />
                      )}
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
            {rowActions ? (
              <th scope="col" className="w-12 px-3">
                <span className="sr-only">Actions</span>
              </th>
            ) : null}
          </tr>
        </thead>
        {/* Arrow keys move focus between the rows' primary buttons (roving tab stop). */}
        {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
        <tbody ref={bodyRef} onKeyDown={moveFocus}>
          {loading
            ? Array.from({ length: Math.max(1, Math.round(skeletonRows)) }, (_, i) => (
                <tr key={`skeleton-${String(i)}`} className="border-b border-border last:border-0">
                  {columns.map((column) => (
                    <td
                      key={column.id}
                      className={cx(cellHeight, "px-3 first:pl-4", column.hideBelow && HIDE[column.hideBelow])}
                    >
                      <Skeleton className={cx("h-3", column.align === "end" ? "ml-auto w-12" : "w-3/4 max-w-40")} />
                    </td>
                  ))}
                  {rowActions ? <td className={cellHeight} /> : null}
                </tr>
              ))
            : null}
          {!loading && sorted.length === 0 && empty !== undefined ? (
            <tr>
              <td colSpan={columns.length + (rowActions ? 1 : 0)} className="p-4">
                {empty}
              </td>
            </tr>
          ) : null}
          {!loading
            ? sorted.map((row) => {
                const id = getRowId(row);
                return (
                  // A click anywhere on the row is a pointer convenience; keyboard users reach
                  // the same action through the row's primary button.
                  <tr
                    key={id}
                    onClick={(event) => onRowClick(event, row)}
                    className={cx(
                      "border-b border-border last:border-0",
                      onRowActivate && "cursor-pointer hover:bg-surface-hover",
                    )}
                  >
                    {columns.map((column, index) => {
                      const content = column.cell(row);
                      return (
                        <td
                          key={column.id}
                          className={cx(
                            cellHeight,
                            "px-3 whitespace-nowrap text-fg first:pl-4",
                            column.align === "end" && "text-right",
                            column.mono && "mono text-12",
                            column.hideBelow && HIDE[column.hideBelow],
                          )}
                        >
                          {index === 0 && onRowActivate ? (
                            <button
                              type="button"
                              data-row-primary=""
                              data-row-id={id}
                              tabIndex={id === tabbableRow ? 0 : -1}
                              onFocus={() => setFocusedRow(id)}
                              onClick={() => onRowActivate(row)}
                              className="-mx-1.5 max-w-full cursor-pointer truncate rounded-control px-1.5 py-0.5 text-left font-medium text-fg focus-visible:outline-2 focus-visible:outline-focus"
                            >
                              {content}
                            </button>
                          ) : (
                            content
                          )}
                        </td>
                      );
                    })}
                    {rowActions ? <td className={cx(cellHeight, "px-2 text-right")}>{rowActions(row)}</td> : null}
                  </tr>
                );
              })
            : null}
        </tbody>
      </table>
    </div>
  );
}

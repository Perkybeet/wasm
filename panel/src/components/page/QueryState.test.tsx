import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/errors";
import { expectNoAxeViolations } from "../../test/axe";
import { ErrorBlock, QueryState } from "./QueryState";
import type { QueryLike } from "./QueryState";

function query<T>(overrides: Partial<QueryLike<T>>): QueryLike<T> {
  return { data: undefined, error: null, isPending: false, isError: false, ...overrides };
}

const FAILURE = new ApiError(
  502,
  "internal",
  "certbot: error: unrecognized arguments: --dry",
  "Update certbot with `apt install certbot`.",
);

function renderState(state: QueryLike<string[]>) {
  return render(
    <QueryState
      query={state}
      label="applications"
      skeleton={<div data-testid="skeleton" />}
      isEmpty={(rows) => rows.length === 0}
      empty={<p>No applications yet</p>}
    >
      {(rows) => (
        <ul>
          {rows.map((row) => (
            <li key={row}>{row}</li>
          ))}
        </ul>
      )}
    </QueryState>,
  );
}

describe("QueryState", () => {
  it("shows the skeleton while loading, busy and named for screen readers", () => {
    const { container } = renderState(query({ isPending: true }));
    expect(screen.getByTestId("skeleton")).toBeInTheDocument();
    expect(screen.getByText("Loading applications")).toHaveClass("sr-only");
    expect(container.firstElementChild).toHaveAttribute("aria-busy", "true");
  });

  it("shows a failure with the fix above and the system's words verbatim below", async () => {
    const refetch = vi.fn();
    renderState(query({ isError: true, error: FAILURE, refetch }));
    expect(screen.getByText("Could not load applications")).toBeInTheDocument();
    const hint = screen.getByText("Update certbot with `apt install certbot`.");
    const detail = screen.getByText("certbot: error: unrecognized arguments: --dry");
    expect(detail.tagName).toBe("PRE");
    // The fix comes first in reading order.
    expect(hint.compareDocumentPosition(detail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("falls back to the page's own hint when the backend gives none", () => {
    render(
      <QueryState query={query<string>({ isError: true, error: new Error("socket hang up") })} label="metrics" skeleton={null} errorHint="Check that the panel is running.">
        {(data) => data}
      </QueryState>,
    );
    expect(screen.getByText("Check that the panel is running.")).toBeInTheDocument();
    expect(screen.getByText("socket hang up")).toBeInTheDocument();
  });

  it("shows the empty state when there is nothing to show", () => {
    renderState(query({ data: [] }));
    expect(screen.getByText("No applications yet")).toBeInTheDocument();
  });

  it("shows the content once loaded", () => {
    renderState(query({ data: ["picconia.com", "cittek.es"] }));
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("keeps the last answer on screen when a refresh fails, and says so", () => {
    renderState(query({ data: ["picconia.com"], isError: true, error: FAILURE }));
    expect(screen.getByText("picconia.com")).toBeInTheDocument();
    expect(screen.getByText(/Could not refresh applications/)).toBeInTheDocument();
  });

  it("has no accessibility violations in any state", async () => {
    const { container, rerender } = renderState(query({ isPending: true }));
    await expectNoAxeViolations(container);
    rerender(
      <QueryState query={query<string[]>({ isError: true, error: FAILURE, refetch: vi.fn() })} label="applications" skeleton={null}>
        {() => null}
      </QueryState>,
    );
    await expectNoAxeViolations(container);
  });
});

describe("ErrorBlock", () => {
  it("interrupts only when it reports something the operator just did", () => {
    const { rerender } = render(<ErrorBlock error={FAILURE} title="Restart failed" />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    rerender(<ErrorBlock error={FAILURE} title="Restart failed" live />);
    expect(screen.getByRole("alert")).toHaveTextContent("Restart failed");
  });
});

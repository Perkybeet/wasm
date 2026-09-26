import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { KeyValueList, KeyValueListSkeleton } from "./KeyValueList";

function stubClipboard(writeText: (text: string) => Promise<void>) {
  vi.stubGlobal("isSecureContext", true);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
}

const ITEMS = [
  { label: "Port", value: 3001 },
  { label: "Directory", value: "/var/www/apps/picconia.com" },
  { label: "Starts at boot", value: "Yes", mono: false, copy: false as const },
  { label: "PID", value: null },
];

describe("KeyValueList", () => {
  it("is a definition list: each label names its value", () => {
    render(<KeyValueList items={ITEMS} />);
    const terms = screen.getAllByRole("term").map((term) => term.textContent);
    expect(terms).toEqual(["Port", "Directory", "Starts at boot", "PID"]);
    const directory = screen.getByText("/var/www/apps/picconia.com");
    expect(directory).toHaveClass("mono");
    expect(directory.closest("dd")).not.toBeNull();
  });

  it("says a missing value in words", () => {
    render(<KeyValueList items={ITEMS} empty="Not running" />);
    expect(screen.getByText("Not running")).toBeInTheDocument();
  });

  it("copies system values exactly, and offers no copy where there is nothing to copy", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    stubClipboard(writeText);
    render(<KeyValueList items={ITEMS} />);
    await userEvent.click(screen.getByRole("button", { name: "Copy directory" }));
    expect(writeText).toHaveBeenCalledWith("/var/www/apps/picconia.com");
    expect(screen.getByRole("button", { name: "Copy port" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy starts at boot" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy pid" })).not.toBeInTheDocument();
  });

  it("copies an explicit value for an element", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    stubClipboard(writeText);
    render(<KeyValueList items={[{ label: "Commit", value: <strong>c07d5e3</strong>, copy: "c07d5e3a91" }]} />);
    await userEvent.click(screen.getByRole("button", { name: "Copy commit" }));
    expect(writeText).toHaveBeenCalledWith("c07d5e3a91");
  });

  it("keeps its shape while loading, hidden from assistive technology", () => {
    const { container } = render(<KeyValueListSkeleton rows={3} />);
    expect(container.firstElementChild).toHaveAttribute("aria-hidden", "true");
    expect(container.firstElementChild?.children).toHaveLength(3);
  });

  it("reserves a second line for the rows that will carry a note", () => {
    const { container } = render(<KeyValueListSkeleton rows={3} hints={[1]} />);
    const rows = Array.from(container.firstElementChild?.children ?? []);
    // Each row's value column: one line, or the line and the note's.
    expect(rows.map((row) => row.lastElementChild?.children.length)).toEqual([1, 2, 1]);
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<KeyValueList items={[...ITEMS, { label: "Note", value: "x", hint: "Set by MemoryMax" }]} />);
    await expectNoAxeViolations(container);
  });
});

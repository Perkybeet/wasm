import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { CommandPalette, filterCommands } from "./CommandPalette";
import type { Command } from "./CommandPalette";

function commands(run = vi.fn()): Command[] {
  return [
    { id: "p1", group: "Pages", label: "Overview", kind: "navigate", run },
    { id: "p2", group: "Pages", label: "Databases", keywords: "postgres mysql", kind: "navigate", run },
    { id: "a1", group: "Applications", label: "shop.example.com", status: "running", kind: "navigate", run },
    { id: "a2", group: "Applications", label: "admin.example.com", status: "failed", kind: "navigate", run },
    { id: "x1", group: "Actions", label: "Keyboard shortcuts", shortcut: ["?"], kind: "action", run },
  ];
}

describe("filterCommands", () => {
  it("puts the best match first, across groups", () => {
    const [first] = filterCommands(commands(), "shop");
    expect(first?.group).toBe("Applications");
    expect(first?.items.map((item) => item.label)).toEqual(["shop.example.com"]);
  });

  it("finds a command by its keywords", () => {
    const results = filterCommands(commands(), "postgres");
    expect(results.flatMap((group) => group.items.map((item) => item.label))).toEqual(["Databases"]);
  });

  it("matches scattered letters only when there are several", () => {
    expect(filterCommands(commands(), "ovw").flatMap((group) => group.items.map((item) => item.label))).toEqual(["Overview"]);
    expect(filterCommands(commands(), "zq")).toEqual([]);
  });
});

describe("CommandPalette", () => {
  it("is a combobox whose active option is announced through aria-activedescendant", async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onOpenChange={() => undefined} commands={commands()} />);
    const input = await screen.findByRole("combobox", { name: "Search pages, applications and actions" });
    expect(input).toHaveFocus();
    const first = screen.getByRole("option", { name: "Overview" });
    expect(input).toHaveAttribute("aria-activedescendant", first.id);
    expect(first).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowDown}");
    const second = screen.getByRole("option", { name: "Databases" });
    expect(input).toHaveAttribute("aria-activedescendant", second.id);
    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(input).toHaveAttribute("aria-activedescendant", screen.getByRole("option", { name: "Keyboard shortcuts" }).id);
  });

  it("filters as the operator types and runs the chosen command with Enter", async () => {
    const user = userEvent.setup();
    const run = vi.fn();
    const onOpenChange = vi.fn();
    render(<CommandPalette open onOpenChange={onOpenChange} commands={commands(run)} />);
    await user.type(await screen.findByRole("combobox"), "admin");
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual(["admin.example.comFailed"]);
    await user.keyboard("{Enter}");
    expect(run).toHaveBeenCalledOnce();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("says when nothing matches, without an empty listbox", async () => {
    const user = userEvent.setup();
    render(<CommandPalette open onOpenChange={() => undefined} commands={commands()} />);
    const input = await screen.findByRole("combobox");
    await user.type(input, "nothing like this");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(input).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText('Nothing matches "nothing like this".')).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    render(<CommandPalette open onOpenChange={() => undefined} commands={commands()} />);
    await screen.findByRole("combobox");
    await expectNoAxeViolations(document.body);
  });
});

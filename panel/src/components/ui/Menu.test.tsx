import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RotateCw, Trash2 } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { Menu, MenuGroup, MenuItem, MenuSeparator } from "./Menu";

function Example({ onRestart = vi.fn(), onDelete = vi.fn() }: { onRestart?: () => void; onDelete?: () => void }) {
  return (
    <Menu trigger={<Button>Actions</Button>}>
      <MenuGroup label="example.com">
        <MenuItem icon={<RotateCw />} shortcut={["R"]} onClick={onRestart}>
          Restart
        </MenuItem>
      </MenuGroup>
      <MenuSeparator />
      <MenuItem icon={<Trash2 />} destructive onClick={onDelete}>
        Delete application
      </MenuItem>
      <MenuItem disabled>Stop</MenuItem>
    </Menu>
  );
}

describe("Menu", () => {
  it("opens a menu of actions from its trigger", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    const menu = await screen.findByRole("menu");
    expect(menu).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Restart/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Stop" })).toHaveAttribute("aria-disabled", "true");
  });

  it("runs the chosen item and closes", async () => {
    const onDelete = vi.fn();
    render(<Example onDelete={onDelete} />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Delete application" }));
    expect(onDelete).toHaveBeenCalledOnce();
    await waitFor(() => {
      expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    });
  });

  it("is operable from the keyboard", async () => {
    const onRestart = vi.fn();
    render(<Example onRestart={onRestart} />);
    screen.getByRole("button", { name: "Actions" }).focus();
    await userEvent.keyboard("{Enter}");
    await screen.findByRole("menu");
    // Opening from the keyboard lands on the first item.
    await waitFor(() => {
      expect(screen.getByRole("menuitem", { name: /Restart/ })).toHaveFocus();
    });
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "Delete application" })).toHaveFocus();
    await userEvent.keyboard("{ArrowUp}{Enter}");
    expect(onRestart).toHaveBeenCalledOnce();
  });

  it("has no accessibility violations when open", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Actions" }));
    await screen.findByRole("menu");
    await expectNoAxeViolations(document.body);
  });
});

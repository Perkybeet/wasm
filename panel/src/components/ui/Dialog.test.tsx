import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { Dialog, DialogClose, MODAL_POPUP, MODAL_VIEWPORT } from "./Dialog";
import { Field } from "./Field";
import { Input } from "./Input";

function Example() {
  return (
    <Dialog
      title="Rename application"
      description="The service, site and certificate follow the new name."
      trigger={<Button>Rename</Button>}
      footer={<DialogClose render={<Button>Cancel</Button>} />}
    >
      <Field label="New domain">
        <Input />
      </Field>
    </Dialog>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Dialog", () => {
  it("opens from its trigger with a name and description", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    const dialog = await screen.findByRole("dialog", { name: "Rename application" });
    expect(dialog).toHaveAccessibleDescription(/follow the new name/);
  });

  it("moves focus inside and returns it to the trigger on Escape", async () => {
    render(<Example />);
    const trigger = screen.getByRole("button", { name: "Rename" });
    await userEvent.click(trigger);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => {
      expect(dialog).toContainElement(document.activeElement as HTMLElement);
    });
    await userEvent.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();
  });

  it("closes from its close controls", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("caps its height by the space its top offset leaves, so the footer stays on screen", () => {
    // Centred with a 16px margin on a phone; 12dvh from the top and 16px below on wider screens.
    expect(MODAL_VIEWPORT).toContain("p-4");
    expect(MODAL_VIEWPORT).toContain("sm:pt-[12dvh]");
    expect(MODAL_POPUP).toContain("max-h-[calc(100dvh-2rem)]");
    expect(MODAL_POPUP).toContain("sm:max-h-[calc(88dvh-1rem)]");
  });

  it("scrolls a read-only body taller than the screen from the keyboard, and keeps the footer", async () => {
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1200);
    render(
      <Dialog
        title="Migration plan"
        defaultOpen
        footer={<DialogClose render={<Button>Cancel</Button>} />}
      >
        <ol>
          {Array.from({ length: 40 }, (_, index) => (
            <li key={index}>{`Step ${String(index + 1)}`}</li>
          ))}
        </ol>
      </Dialog>,
    );
    const dialog = await screen.findByRole("dialog", { name: "Migration plan" });
    const body = await screen.findByRole("region", { name: "Migration plan" });
    expect(body).toHaveAttribute("tabindex", "0");
    expect(dialog).toContainElement(screen.getByRole("button", { name: "Cancel" }));
  });

  it("adds no tab stop to a scrolling body whose controls take focus", async () => {
    vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(300);
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(1200);
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    await screen.findByRole("dialog", { name: "Rename application" });
    expect(screen.queryByRole("region", { name: "Rename application" })).toBeNull();
  });

  it("has no accessibility violations when open", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  });
});

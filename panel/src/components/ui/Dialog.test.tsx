import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { Dialog, DialogClose } from "./Dialog";
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

  it("has no accessibility violations when open", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Rename" }));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  });
});

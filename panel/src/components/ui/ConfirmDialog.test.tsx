import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { ConfirmDialog } from "./ConfirmDialog";

function setup(onConfirm: () => Promise<void>) {
  render(
    <ConfirmDialog
      title="Delete example.com"
      description="Stops the service and removes the site. Backups are kept."
      confirmText="example.com"
      actionLabel="Delete application"
      onConfirm={onConfirm}
      trigger={<Button>Delete</Button>}
    />,
  );
}

async function open() {
  await userEvent.click(screen.getByRole("button", { name: "Delete" }));
  return screen.findByRole("alertdialog", { name: "Delete example.com" });
}

describe("ConfirmDialog", () => {
  it("keeps the action disabled until the name is typed exactly", async () => {
    const onConfirm = vi.fn(() => Promise.resolve());
    setup(onConfirm);
    await open();
    const action = screen.getByRole("button", { name: "Delete application" });
    const input = screen.getByRole("textbox", { name: /Type example.com to confirm/ });
    expect(action).toBeDisabled();
    await userEvent.type(input, "example.co");
    expect(action).toBeDisabled();
    await userEvent.type(input, "M");
    expect(action).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, "example.com");
    expect(action).toBeEnabled();
  });

  it("focuses the name field when it opens", async () => {
    setup(() => Promise.resolve());
    await open();
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: /to confirm/ })).toHaveFocus();
    });
  });

  it("runs the action on Enter once confirmed and closes when it succeeds", async () => {
    const onConfirm = vi.fn(() => Promise.resolve());
    setup(onConfirm);
    await open();
    await userEvent.type(screen.getByRole("textbox", { name: /to confirm/ }), "example.com{Enter}");
    expect(onConfirm).toHaveBeenCalledOnce();
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
  });

  it("stays open and shows the failure verbatim, with the fix above it", async () => {
    const failure = Object.assign(new Error("request failed"), {
      hint: "The unit did not stop. Check its logs, then try again.",
      detail: "Job for wasm-example.com.service canceled.",
    });
    setup(() => Promise.reject(failure));
    await open();
    await userEvent.type(screen.getByRole("textbox", { name: /to confirm/ }), "example.com");
    await userEvent.click(screen.getByRole("button", { name: "Delete application" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The unit did not stop. Check its logs, then try again.");
    expect(alert).toHaveTextContent("Job for wasm-example.com.service canceled.");
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
  });

  it("does not run the action when cancelled", async () => {
    const onConfirm = vi.fn(() => Promise.resolve());
    setup(onConfirm);
    await open();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    });
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("has no accessibility violations when open", async () => {
    setup(() => Promise.resolve());
    await open();
    await expectNoAxeViolations(document.body);
  });
});

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { Popover } from "./Popover";

function Example() {
  return (
    <Popover
      title="What is a release?"
      description="Each deploy builds into its own directory."
      trigger={<Button>Releases</Button>}
    >
      <a href="#docs">Read more</a>
    </Popover>
  );
}

describe("Popover", () => {
  it("opens a named dialog from its trigger and closes on Escape", async () => {
    render(<Example />);
    const trigger = screen.getByRole("button", { name: "Releases" });
    await userEvent.click(trigger);
    const popup = await screen.findByRole("dialog", { name: "What is a release?" });
    expect(popup).toHaveAccessibleDescription("Each deploy builds into its own directory.");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("has no accessibility violations when open", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Releases" }));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  });
});

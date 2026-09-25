import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { Drawer } from "./Drawer";

function Example() {
  return (
    <Drawer
      title="Deployment a1b2c3d"
      description="Fix checkout rounding"
      trigger={<Button>View deployment</Button>}
      footer={<Button>Redeploy</Button>}
    >
      <p>Release 20260925-143012-a1b2c3d</p>
    </Drawer>
  );
}

describe("Drawer", () => {
  it("opens as a named dialog with its content", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "View deployment" }));
    const drawer = await screen.findByRole("dialog", { name: "Deployment a1b2c3d" });
    expect(drawer).toHaveTextContent("Release 20260925-143012-a1b2c3d");
  });

  it("closes from its close button and returns focus", async () => {
    render(<Example />);
    const trigger = screen.getByRole("button", { name: "View deployment" });
    await userEvent.click(trigger);
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(trigger).toHaveFocus();
  });

  it("has no accessibility violations when open", async () => {
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "View deployment" }));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  });
});

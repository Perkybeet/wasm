import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Rocket } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";

describe("Button", () => {
  it("runs its action when pressed", async () => {
    const onClick = vi.fn();
    render(
      <Button variant="primary" icon={<Rocket />} onClick={onClick}>
        Deploy
      </Button>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Deploy" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("is a plain button, never a form submit, unless asked", () => {
    render(<Button>Restart</Button>);
    expect(screen.getByRole("button", { name: "Restart" })).toHaveAttribute("type", "button");
  });

  it("keeps its label and focus while loading, and ignores presses", async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        Deploying
      </Button>,
    );
    const button = screen.getByRole("button", { name: "Deploying" });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveAttribute("aria-disabled", "true");
    button.focus();
    expect(button).toHaveFocus();
    await userEvent.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("does not run when disabled", async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Restart
      </Button>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Restart" }));
    expect(onClick).not.toHaveBeenCalled();
  });

  it("has no accessibility violations in any variant", async () => {
    const { container } = render(
      <div>
        <Button variant="primary">Deploy</Button>
        <Button>Restart</Button>
        <Button variant="ghost">Cancel</Button>
        <Button variant="danger">Delete application</Button>
        <Button loading>Deploying</Button>
        <Button disabled>Stop</Button>
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

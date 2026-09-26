import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Switch } from "./Switch";

describe("Switch", () => {
  it("turns on and off from its label", async () => {
    const onCheckedChange = vi.fn();
    render(<Switch label="Deploy on push" onCheckedChange={onCheckedChange} />);
    const control = screen.getByRole("switch", { name: "Deploy on push" });
    expect(control).toHaveAttribute("aria-checked", "false");
    await userEvent.click(screen.getByText("Deploy on push"));
    expect(control).toHaveAttribute("aria-checked", "true");
    expect(onCheckedChange).toHaveBeenLastCalledWith(true);
  });

  it("follows a controlled value", () => {
    const { rerender } = render(<Switch aria-label="Maintenance page" checked={false} />);
    expect(screen.getByRole("switch", { name: "Maintenance page" })).toHaveAttribute("aria-checked", "false");
    rerender(<Switch aria-label="Maintenance page" checked />);
    expect(screen.getByRole("switch", { name: "Maintenance page" })).toHaveAttribute("aria-checked", "true");
  });

  it("is named by its label and described by its second line", async () => {
    render(<Switch label="Deploy on push" description="Build when main receives a commit." />);
    const control = screen.getByRole("switch", { name: "Deploy on push" });
    expect(control).toHaveAccessibleDescription("Build when main receives a commit.");
    await userEvent.click(screen.getByText("Build when main receives a commit."));
    expect(control).toHaveAttribute("aria-checked", "true");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <Switch label="Deploy on push" description="Build when main receives a commit." defaultChecked />
        <Switch aria-label="Maintenance page" />
        <Switch label="Two-factor authentication" disabled defaultChecked />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

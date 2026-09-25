import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Checkbox } from "./Checkbox";

describe("Checkbox", () => {
  it("toggles from its label", async () => {
    const onCheckedChange = vi.fn();
    render(<Checkbox label="Include www" onCheckedChange={onCheckedChange} />);
    const box = screen.getByRole("checkbox", { name: "Include www" });
    expect(box).toHaveAttribute("aria-checked", "false");
    await userEvent.click(screen.getByText("Include www"));
    expect(onCheckedChange).toHaveBeenCalledWith(true);
    expect(box).toHaveAttribute("aria-checked", "true");
  });

  it("toggles with the space bar", async () => {
    render(<Checkbox label="Back up databases" />);
    const box = screen.getByRole("checkbox", { name: "Back up databases" });
    box.focus();
    await userEvent.keyboard(" ");
    expect(box).toHaveAttribute("aria-checked", "true");
  });

  it("reports a mixed state", () => {
    render(<Checkbox label="All volumes" indeterminate />);
    expect(screen.getByRole("checkbox", { name: "All volumes" })).toHaveAttribute("aria-checked", "mixed");
  });

  it("ignores presses when disabled", async () => {
    const onCheckedChange = vi.fn();
    render(<Checkbox label="Encrypt" disabled onCheckedChange={onCheckedChange} />);
    await userEvent.click(screen.getByText("Encrypt"));
    expect(onCheckedChange).not.toHaveBeenCalled();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <Checkbox label="Include www" description="Redirect www to the apex." defaultChecked />
        <Checkbox label="All volumes" indeterminate />
        <Checkbox aria-label="Select shop.arenna.dev" />
        <Checkbox label="Encrypt" disabled />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Search } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Input } from "./Input";

describe("Input", () => {
  it("reports each change", async () => {
    const onValueChange = vi.fn();
    render(<Input aria-label="Domain" onValueChange={onValueChange} />);
    await userEvent.type(screen.getByRole("textbox", { name: "Domain" }), "ab");
    expect(onValueChange).toHaveBeenLastCalledWith("ab", expect.anything());
  });

  it("shows fixed text around the value without making it part of the value", async () => {
    render(<Input aria-label="Repository" prefix="https://" suffix=".git" />);
    const input = screen.getByRole("textbox", { name: "Repository" });
    await userEvent.type(input, "github.com/you/app");
    expect(input).toHaveValue("github.com/you/app");
    expect(screen.getByText("https://")).toBeInTheDocument();
    expect(screen.getByText(".git")).toBeInTheDocument();
  });

  it("sets system values in mono", () => {
    render(<Input aria-label="Port" mono />);
    expect(screen.getByRole("textbox", { name: "Port" })).toHaveClass("mono");
  });

  it("can be disabled", () => {
    render(<Input aria-label="Domain" disabled />);
    expect(screen.getByRole("textbox", { name: "Domain" })).toBeDisabled();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <Input aria-label="Search" icon={<Search />} placeholder="Search" />
        <Input aria-label="Repository" prefix="https://" />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

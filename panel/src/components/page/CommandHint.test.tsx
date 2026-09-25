import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { CommandHint } from "./CommandHint";

describe("CommandHint", () => {
  it("shows the command behind a prompt that is not part of it", () => {
    render(<CommandHint command="wasm status picconia.com" label="From a terminal" />);
    const code = screen.getByText("wasm status picconia.com", { exact: false });
    expect(code.tagName).toBe("CODE");
    expect(code.querySelector('[aria-hidden="true"]')).toHaveTextContent("$");
    expect(screen.getByText("From a terminal")).toBeInTheDocument();
  });

  it("copies the command alone", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    vi.stubGlobal("isSecureContext", true);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<CommandHint command="wasm update picconia.com" />);
    await userEvent.click(screen.getByRole("button", { name: "Copy command" }));
    expect(writeText).toHaveBeenCalledWith("wasm update picconia.com");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<CommandHint command="wasm list" label="From a terminal" />);
    await expectNoAxeViolations(container);
  });
});

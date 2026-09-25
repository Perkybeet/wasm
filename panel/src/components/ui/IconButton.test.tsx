import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RefreshCw, WrapText } from "lucide-react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { IconButton } from "./IconButton";

describe("IconButton", () => {
  it("is named by its label", async () => {
    const onClick = vi.fn();
    render(<IconButton label="Refresh" icon={<RefreshCw />} onClick={onClick} />);
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("exposes a toggle's state", () => {
    render(<IconButton label="Wrap lines" icon={<WrapText />} pressed />);
    expect(screen.getByRole("button", { name: "Wrap lines" })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows its label as a tooltip on keyboard focus", async () => {
    render(<IconButton label="Refresh" icon={<RefreshCw />} shortcut={["R"]} />);
    await userEvent.tab();
    expect(await screen.findByText("Refresh", {}, { timeout: 2000 })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <IconButton label="Refresh" icon={<RefreshCw />} />
        <IconButton label="Wrap lines" icon={<WrapText />} pressed={false} variant="secondary" />
        <IconButton label="Start" icon={<RefreshCw />} disabled tooltip={false} />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

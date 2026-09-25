import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { formatBytes, formatPercent } from "../../lib/format";
import { expectNoAxeViolations } from "../../test/axe";
import { ResourceMeter } from "./ResourceMeter";

const MB = 1024 * 1024;

describe("ResourceMeter", () => {
  it("measures the reading against its limit and says both", () => {
    render(<ResourceMeter label="Memory" value={96 * MB} limit={512 * MB} format={formatBytes} />);
    const meter = screen.getByRole("meter", { name: "Memory" });
    expect(meter).toHaveAttribute("aria-valuetext", "96 MB of 512 MB");
  });

  it("turns amber, then red, as the limit nears", () => {
    const { container, rerender } = render(<ResourceMeter label="Memory" value={400 * MB} limit={512 * MB} format={formatBytes} />);
    expect(container.querySelector("[data-level]")).toHaveAttribute("data-level", "warn");
    rerender(<ResourceMeter label="Memory" value={500 * MB} limit={512 * MB} format={formatBytes} />);
    expect(container.querySelector("[data-level]")).toHaveAttribute("data-level", "fail");
  });

  it("stands alone without a limit instead of drawing a bar with no end", () => {
    render(<ResourceMeter label="CPU" value={4.2} format={formatPercent} />);
    expect(screen.queryByRole("meter")).not.toBeInTheDocument();
    expect(screen.getByText("4.2%")).toBeInTheDocument();
    expect(screen.getByText("No limit set")).toBeInTheDocument();
  });

  it("says why there is no reading", () => {
    render(<ResourceMeter label="CPU" value={null} limit={50} format={formatPercent} missing="Not running" />);
    expect(screen.getByText("Not running")).toBeInTheDocument();
    expect(screen.getByText("Limit 50%")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <ResourceMeter label="Memory" value={96 * MB} limit={512 * MB} format={formatBytes} />
        <ResourceMeter label="CPU" value={null} format={formatPercent} />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

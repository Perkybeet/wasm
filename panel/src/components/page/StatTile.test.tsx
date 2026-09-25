import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { StatusPill } from "../ui/StatusPill";
import { StatTile } from "./StatTile";

describe("StatTile", () => {
  it("shows the label, the value and its context", () => {
    render(<StatTile label="Current release" value="c07d5e3" mono detail="main, deployed 3m ago" />);
    expect(screen.getByText("Current release")).toBeInTheDocument();
    expect(screen.getByText("c07d5e3")).toHaveClass("mono");
    expect(screen.getByText("main, deployed 3m ago")).toBeInTheDocument();
  });

  it("sets a quantity in the interface face, not mono", () => {
    render(<StatTile label="Applications" value={8} />);
    expect(screen.getByText("8")).not.toHaveClass("mono");
  });

  it("takes an element as its value", () => {
    render(<StatTile label="State" value={<StatusPill state="running" />} />);
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <StatTile label="Current release" value="c07d5e3" mono detail="main" />
        <StatTile label="State" value={<StatusPill state="failed" />} />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

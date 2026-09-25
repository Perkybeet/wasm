import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Badge } from "./Badge";

describe("Badge", () => {
  it("is neutral unless it reports a state", () => {
    render(
      <div>
        <Badge>Next.js</Badge>
        <Badge tone="fail">Expired</Badge>
      </div>,
    );
    expect(screen.getByText("Next.js")).toHaveClass("text-fg-muted");
    expect(screen.getByText("Expired")).toHaveClass("text-fail");
  });

  it("sets system values in mono", () => {
    render(<Badge mono>node 22.23.3</Badge>);
    expect(screen.getByText("node 22.23.3")).toHaveClass("mono");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <Badge>Next.js</Badge>
        <Badge tone="accent">Primary domain</Badge>
        <Badge tone="ok">Valid</Badge>
        <Badge tone="warn">Expires in 6 d</Badge>
        <Badge tone="fail">Expired</Badge>
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

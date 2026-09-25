import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Mono } from "./Mono";

describe("Mono", () => {
  it("sets a system value in mono and keeps page translation away from it", () => {
    render(<Mono>/var/www/apps/shop/current</Mono>);
    const value = screen.getByText("/var/www/apps/shop/current");
    expect(value).toHaveClass("mono");
    expect(value).toHaveAttribute("translate", "no");
  });

  it("keeps the full value available when truncated", () => {
    render(<Mono truncate>20260925-143012-a1b2c3d</Mono>);
    expect(screen.getByText("20260925-143012-a1b2c3d")).toHaveAttribute("title", "20260925-143012-a1b2c3d");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <p>
        Listening on <Mono>:3004</Mono> as <Mono tone="muted">wasm-shop.service</Mono>
      </p>,
    );
    await expectNoAxeViolations(container);
  });
});

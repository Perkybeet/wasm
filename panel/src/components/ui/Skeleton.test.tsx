import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Skeleton, SkeletonText } from "./Skeleton";

describe("Skeleton", () => {
  it("is hidden from assistive technology; the busy region speaks for it", () => {
    const { container } = render(
      <div aria-busy="true">
        <Skeleton className="h-4 w-32" />
        <SkeletonText lines={3} />
      </div>,
    );
    for (const node of container.querySelectorAll("div > span")) expect(node).toHaveAttribute("aria-hidden", "true");
  });

  it("ends a paragraph with a shorter line", () => {
    const { container } = render(<SkeletonText lines={3} />);
    const lines = container.querySelectorAll("span > span");
    expect(lines).toHaveLength(3);
    expect(lines[2]).toHaveClass("w-3/5");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <section aria-busy="true" aria-label="Applications">
        <SkeletonText />
      </section>,
    );
    await expectNoAxeViolations(container);
  });
});

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Spinner } from "./Spinner";

describe("Spinner", () => {
  it("announces what is loading when labelled", () => {
    render(<Spinner label="Loading applications" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading applications");
  });

  it("is decorative without a label", () => {
    const { container } = render(<Spinner />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <div>
        <Spinner label="Loading applications" />
        <Spinner size={12} />
      </div>,
    );
    await expectNoAxeViolations(container);
  });
});

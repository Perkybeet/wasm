import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Kbd } from "./Kbd";

describe("Kbd", () => {
  it("marks up a key as keyboard input", () => {
    render(
      <p>
        Press <Kbd>Ctrl</Kbd> <Kbd>K</Kbd> to search
      </p>,
    );
    expect(screen.getByText("Ctrl").tagName).toBe("KBD");
    expect(screen.getByText("K").tagName).toBe("KBD");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <p>
        <Kbd>g</Kbd> <Kbd>a</Kbd> Applications
      </p>,
    );
    await expectNoAxeViolations(container);
  });
});

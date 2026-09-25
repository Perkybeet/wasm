import { render, screen } from "@testing-library/react";
import { Boxes } from "lucide-react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { EmptyState } from "./EmptyState";

const COMMAND = "wasm create -d example.com -s git@github.com:you/app.git";

describe("EmptyState", () => {
  it("says what the place is for and offers the action and the command", () => {
    render(
      <EmptyState
        icon={<Boxes />}
        title="No applications yet"
        description="Deploy a repository and WASM builds and runs it."
        action={<Button variant="primary">New application</Button>}
        command={COMMAND}
      />,
    );
    expect(screen.getByRole("heading", { name: "No applications yet" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New application" })).toBeInTheDocument();
    expect(screen.getByText(COMMAND)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <EmptyState icon={<Boxes />} title="No applications yet" description="Deploy one." command={COMMAND} />,
    );
    await expectNoAxeViolations(container);
  });
});

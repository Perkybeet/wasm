import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "../ui/Button";
import { DangerAction, DangerZone } from "./DangerZone";

function Zone() {
  return (
    <DangerZone description="These cannot be undone.">
      <DangerAction
        title="Delete this application"
        description="Stops the service, removes its files, site and certificate."
        action={<Button variant="danger">Delete application</Button>}
      />
      <DangerAction
        title="Remove the webhook"
        description="Pushes stop deploying."
        action={<Button variant="danger">Remove webhook</Button>}
      />
    </DangerZone>
  );
}

describe("DangerZone", () => {
  it("is its own region, each action titled and explained next to its button", () => {
    render(<Zone />);
    expect(screen.getByRole("region", { name: "Danger zone" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Delete this application" })).toBeInTheDocument();
    expect(screen.getByText("Stops the service, removes its files, site and certificate.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete application" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<Zone />);
    await expectNoAxeViolations(container);
  });
});

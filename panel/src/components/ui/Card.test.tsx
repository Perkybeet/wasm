import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { Card } from "./Card";

describe("Card", () => {
  it("titles its content with a heading at the requested level", () => {
    render(
      <Card title="Resources" description="Against the limits set for this app" level={2}>
        <p>CPU 38%</p>
      </Card>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Resources" })).toBeInTheDocument();
    expect(screen.getByText("Against the limits set for this app")).toBeInTheDocument();
  });

  it("places actions beside the title and a footer below", () => {
    render(
      <Card title="Deploy on push" actions={<Button>Edit</Button>} footer={<Button>Save</Button>}>
        <p>Webhook</p>
      </Card>,
    );
    expect(screen.getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(screen.getByRole("contentinfo")).toContainElement(screen.getByRole("button", { name: "Save" }));
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <main>
        <Card title="Resources" actions={<Button>Edit limits</Button>} footer={<Button>Save</Button>}>
          <p>CPU 38%</p>
        </Card>
        <Card padding="none">
          <p>Edge to edge</p>
        </Card>
      </main>,
    );
    await expectNoAxeViolations(container);
  });
});

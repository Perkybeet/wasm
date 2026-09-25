import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "../ui/Button";
import { Section, Sections } from "./Section";

describe("Section", () => {
  it("is a region named by its heading, a level below the page title", () => {
    render(
      <Section title="Recent deployments" description="The last eight, across every app.">
        <p>rows</p>
      </Section>,
    );
    expect(screen.getByRole("region", { name: "Recent deployments" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Recent deployments" })).toBeInTheDocument();
    expect(screen.getByText("The last eight, across every app.")).toBeInTheDocument();
  });

  it("nests one level deeper when asked", () => {
    render(
      <Section title="Runtime" level={3}>
        <p>facts</p>
      </Section>,
    );
    expect(screen.getByRole("heading", { level: 3, name: "Runtime" })).toBeInTheDocument();
  });

  it("puts the section's actions beside its title", () => {
    render(
      <Section title="Machine" actions={<Button>View server</Button>} badge={<span>live</span>}>
        <p>charts</p>
      </Section>,
    );
    const header = screen.getByRole("heading", { name: "Machine" }).closest("header");
    expect(header).toContainElement(screen.getByRole("button", { name: "View server" }));
    expect(header).toContainElement(screen.getByText("live"));
  });

  it("stacks sections 32px apart", () => {
    const { container } = render(
      <Sections>
        <Section title="One">
          <p>1</p>
        </Section>
        <Section title="Two">
          <p>2</p>
        </Section>
      </Sections>,
    );
    expect(container.firstElementChild).toHaveClass("gap-8");
  });

  it("has no accessibility violations", async () => {
    const { container } = render(
      <Sections>
        <Section title="Needs attention" description="Problems first." actions={<Button>Refresh</Button>}>
          <p>Nothing needs attention.</p>
        </Section>
      </Sections>,
    );
    await expectNoAxeViolations(container);
  });
});

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Tab, TabList, TabPanel, Tabs } from "./Tabs";

function Example({ onValueChange }: { onValueChange?: (value: string) => void }) {
  return (
    <Tabs defaultValue="overview" {...(onValueChange ? { onValueChange } : {})}>
      <TabList aria-label="Application sections">
        <Tab value="overview">Overview</Tab>
        <Tab value="deployments" count={12}>
          Deployments
        </Tab>
        <Tab value="logs">Logs</Tab>
        <Tab value="settings" disabled>
          Settings
        </Tab>
      </TabList>
      <TabPanel value="overview">Overview content</TabPanel>
      <TabPanel value="deployments">Deployments content</TabPanel>
      <TabPanel value="logs">Logs content</TabPanel>
      <TabPanel value="settings">Settings content</TabPanel>
    </Tabs>
  );
}

describe("Tabs", () => {
  it("shows the panel of the selected tab", async () => {
    const onValueChange = vi.fn();
    render(<Example onValueChange={onValueChange} />);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Overview content");
    await userEvent.click(screen.getByRole("tab", { name: /Deployments/ }));
    expect(onValueChange).toHaveBeenCalledWith("deployments");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Deployments content");
  });

  it("moves between tabs with the arrow keys", async () => {
    render(<Example />);
    await userEvent.tab();
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveFocus();
    await userEvent.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /Deployments/ })).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("tab", { name: /Deployments/ })).toHaveAttribute("aria-selected", "true");
  });

  it("names the tab list and shows counts in the tab name", () => {
    render(<Example />);
    expect(screen.getByRole("tablist", { name: "Application sections" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Deployments 12" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = render(<Example />);
    await expectNoAxeViolations(container);
  });
});

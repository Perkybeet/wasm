import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { Button } from "./Button";
import { Tooltip, TooltipProvider } from "./Tooltip";

describe("Tooltip", () => {
  it("appears on keyboard focus and goes on Escape", async () => {
    render(
      <TooltipProvider>
        <Tooltip content="Last deployed 12 min ago" shortcut={["D"]}>
          <Button>Deploy</Button>
        </Tooltip>
      </TooltipProvider>,
    );
    await userEvent.tab();
    expect(await screen.findByText("Last deployed 12 min ago", {}, { timeout: 2000 })).toBeInTheDocument();
    expect(screen.getByText("D").tagName).toBe("KBD");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByText("Last deployed 12 min ago")).not.toBeInTheDocument();
    });
  });

  it("never opens when disabled", async () => {
    render(
      <Tooltip content="Hidden" disabled>
        <Button>Deploy</Button>
      </Tooltip>,
    );
    await userEvent.tab();
    await new Promise((resolve) => setTimeout(resolve, 700));
    expect(screen.queryByText("Hidden")).not.toBeInTheDocument();
  });

  it("has no accessibility violations when shown", async () => {
    render(
      <Tooltip content="Last deployed 12 min ago">
        <Button>Deploy</Button>
      </Tooltip>,
    );
    await userEvent.tab();
    await screen.findByText("Last deployed 12 min ago", {}, { timeout: 2000 });
    await expectNoAxeViolations(document.body);
  });
});

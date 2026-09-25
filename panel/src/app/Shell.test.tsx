import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../test/axe";
import { renderConsole } from "../test/console";
import { ANONYMOUS, FakeEventSource, MACHINE, fakeBackend, json, signedInRoutes } from "../test/fakes";
import { THEME_STORAGE_KEY } from "./theme";

async function shellAt(path: string) {
  fakeBackend(signedInRoutes());
  const harness = renderConsole(path);
  await screen.findByRole("heading", { level: 1 });
  return harness;
}

describe("the shell", () => {
  it("frames every page with landmarks, the machine strip and the page's own header", async () => {
    await shellAt("/apps");
    // Testing Library calls every <header> a banner; axe (below) checks there is only one.
    expect(screen.getByRole("group", { name: "This machine" }).closest("header")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toContainElement(screen.getByRole("heading", { level: 1, name: "Applications" }));
    const strip = screen.getByRole("group", { name: "This machine" });
    expect(await within(strip).findByText("web-01")).toBeInTheDocument();
    expect(within(strip).getByRole("meter", { name: "CPU" })).toBeInTheDocument();
    expect(document.title).toBe("Applications - web-01 - WASM");
  });

  it("has no accessibility violations", async () => {
    await shellAt("/apps");
    await within(screen.getByRole("group", { name: "This machine" })).findByText("web-01");
    await expectNoAxeViolations(document.body, { page: true });
  });

  it("keeps the machine strip current from the machine event", async () => {
    await shellAt("/");
    const strip = screen.getByRole("group", { name: "This machine" });
    await within(strip).findByText("web-01");
    act(() => {
      FakeEventSource.latest().open();
      FakeEventSource.latest().emit("machine", { ...MACHINE, hostname: "web-02", units: { running: 9, failed: 3, stopped: 2 } });
    });
    expect(await within(strip).findByText("web-02")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Services 3 failed" })).toBeInTheDocument();
    expect(within(strip).getByRole("link", { name: "Units 9 running 3 failed 2 stopped" })).toBeInTheDocument();
  });

  it("sends an anonymous visitor to sign in, remembering where they were going", async () => {
    fakeBackend({ "GET /api/auth/session": () => json(200, ANONYMOUS) });
    const { location } = renderConsole("/apps/shop.example.com/logs");
    await screen.findByRole("heading", { level: 1, name: "Sign in" });
    expect(location().pathname).toBe("/login");
    expect(location().search).toEqual({ next: "/apps/shop.example.com/logs" });
    expect(screen.queryByText("Your session expired. Sign in again to continue where you left off.")).toBeNull();
  });
});

describe("keyboard", () => {
  it("offers a skip link first that moves focus to the page", async () => {
    const { user } = await shellAt("/apps");
    await user.tab();
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("main")).toHaveFocus();
  });

  it("opens the palette with Ctrl+K, filters, and opens the page with Enter", async () => {
    const { user, location } = await shellAt("/apps");
    await user.keyboard("{Control>}k{/Control}");
    const input = await screen.findByRole("combobox", { name: "Search pages, applications and actions" });
    await user.type(input, "datab");
    await user.keyboard("{Enter}");
    await screen.findByRole("heading", { level: 1, name: "Databases" });
    expect(location().pathname).toBe("/databases");
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Databases" })).toHaveFocus();
    });
  });

  it("lists applications in the palette and opens one", async () => {
    const { user, location } = await shellAt("/");
    await user.keyboard("{Control>}k{/Control}");
    await user.type(await screen.findByRole("combobox"), "shop");
    await screen.findByRole("option", { name: /shop\.example\.com/ });
    await user.keyboard("{Enter}");
    await screen.findByRole("heading", { level: 1, name: "shop.example.com" });
    expect(location().pathname).toBe("/apps/shop.example.com");
  });

  it("returns focus to the search button when the palette closes with Escape", async () => {
    const { user } = await shellAt("/apps");
    // The phone layout has its own search button; CSS shows one of the two.
    const trigger = screen.getAllByRole("button", { name: "Search" }).find((button) => button.hasAttribute("aria-keyshortcuts"));
    if (!trigger) throw new Error("No search trigger");
    await user.click(trigger);
    // Named: the applications page has comboboxes of its own (its filters).
    const palette = { name: "Search pages, applications and actions" };
    await screen.findByRole("combobox", palette);
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("combobox", palette)).toBeNull();
    });
    await waitFor(() => {
      expect(trigger).toHaveFocus();
    });
  });

  it("goes to applications with g then a, and focuses the new page's heading", async () => {
    const { user, location } = await shellAt("/");
    await user.keyboard("ga");
    await screen.findByRole("heading", { level: 1, name: "Applications" });
    expect(location().pathname).toBe("/apps");
    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 1, name: "Applications" })).toHaveFocus();
    });
  });

  it("goes to the deployments of the app in view with g then d", async () => {
    const { user, location } = await shellAt("/apps/shop.example.com/logs");
    await user.keyboard("gd");
    await waitFor(() => {
      expect(location().pathname).toBe("/apps/shop.example.com/deployments");
    });
  });

  it("lists the shortcuts with ?", async () => {
    const { user } = await shellAt("/");
    await user.keyboard("?");
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(dialog).getByText("Go to applications")).toBeInTheDocument();
    expect(within(dialog).getByText("Open the command palette")).toBeInTheDocument();
  });

  it("keeps focus on a tab when moving between an app's sections", async () => {
    const { user } = await shellAt("/apps/shop.example.com");
    const logs = screen.getByRole("link", { name: "Logs" });
    await user.click(logs);
    await screen.findByRole("region", { name: "Journal of shop.example.com" });
    expect(screen.getByRole("link", { name: "Logs" })).toHaveFocus();
    expect(screen.getByRole("link", { name: "Logs" })).toHaveAttribute("aria-current", "page");
  });
});

describe("preferences", () => {
  it("switches and remembers the theme", async () => {
    const { user } = await shellAt("/");
    await user.click(screen.getByRole("button", { name: "Session and preferences" }));
    const dark = await screen.findByRole("button", { name: "Dark" });
    await user.click(dark);
    expect(dark).toHaveAttribute("aria-pressed", "true");
    expect(document.documentElement.dataset["theme"]).toBe("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });
});

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { ToastProvider } from "./Toast";
import { toast } from "./toast";

/** The rendered toast. */
function visibleToast(): HTMLElement {
  const element = document.querySelector<HTMLElement>(".toast");
  if (!element) throw new Error("No toast is rendered");
  return element;
}

/** The outermost live regions whose text includes `text`: where a screen reader hears it. */
function liveRegionsSaying(text: string): Element[] {
  return [...document.querySelectorAll('[aria-live]:not([aria-live="off"]), [role="alert"], [role="status"]')].filter(
    (region) => region.textContent.includes(text) && !region.parentElement?.closest('[aria-live]:not([aria-live="off"])'),
  );
}

const FOCUSABLE = ':is(a[href], button, input, select, textarea, [tabindex]):not([tabindex="-1"]):not([disabled])';

/** Focusable elements that assistive technology has been told do not exist. */
function focusablesHiddenFromAssistiveTech(): string[] {
  const found: string[] = [];
  for (const hidden of document.querySelectorAll('[aria-hidden="true"]')) {
    const inside = [...(hidden.matches(FOCUSABLE) ? [hidden] : []), ...hidden.querySelectorAll(FOCUSABLE)];
    for (const element of inside) found.push(element.outerHTML.slice(0, 120));
  }
  return found;
}

afterEach(() => {
  act(() => {
    toast.dismiss();
  });
});

describe("Toast", () => {
  it("announces a failure once, assertively, through a live region", async () => {
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.error("Deploy failed", { description: "The health check did not pass." });
    });
    const alert = await screen.findByRole("alert");
    await waitFor(() => {
      expect(alert).toHaveTextContent("Deploy failedThe health check did not pass.");
    });
    // The visible toast is what is announced: one live region holds it, and there is no
    // second, hidden copy of the words anywhere on the page.
    expect(liveRegionsSaying("Deploy failed")).toEqual([alert]);
    expect(alert).toHaveAttribute("aria-live", "assertive");
    expect(alert).toContainElement(visibleToast());
    expect(screen.getAllByText("Deploy failed")).toHaveLength(1);
  });

  it("announces a success once, politely", async () => {
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.success("Saved backup settings");
    });
    await waitFor(() => {
      expect(visibleToast()).toHaveTextContent("Saved backup settings");
    });
    const regions = liveRegionsSaying("Saved backup settings");
    expect(regions).toHaveLength(1);
    expect(regions[0]).toHaveAttribute("aria-live", "polite");
    expect(regions[0]).toContainElement(visibleToast());
    expect(screen.getAllByText("Saved backup settings")).toHaveLength(1);
    expect(screen.getByRole("alert")).toBeEmptyDOMElement();
  });

  it("never hides a focusable element from assistive technology", async () => {
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.success("Deployed example.com");
      toast.error("Deploy failed", { description: "The health check did not pass.", detail: "502" });
    });
    await waitFor(() => {
      expect(document.querySelectorAll(".toast")).toHaveLength(2);
    });
    // Collapsed stack, nothing focused: the state Base UI used to hide the toasts in.
    expect(focusablesHiddenFromAssistiveTech()).toEqual([]);
    // jsdom has no layout, so axe cannot always settle focusability and files the rule as
    // "incomplete" rather than as a violation; either answer fails here.
    const results = await axe.run(document.body, { runOnly: ["aria-hidden-focus"] });
    expect([...results.violations, ...results.incomplete].map((rule) => rule.id)).toEqual([]);
    for (const dismiss of screen.getAllByRole("button", { name: "Dismiss notification" })) {
      expect(dismiss).toBeVisible();
    }
  });

  it("reports a success with its title and description", async () => {
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.success("Deployed example.com", { description: "a1b2c3d is live." });
    });
    await waitFor(() => {
      expect(visibleToast()).toHaveTextContent("Deployed example.com");
    });
    expect(visibleToast()).toHaveTextContent("a1b2c3d is live.");
  });

  it("shows a failure's system output verbatim and keeps it until dismissed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.error("Deploy failed", { detail: "GET http://127.0.0.1:3004/ returned 502" });
    });
    await waitFor(() => {
      expect(within(visibleToast()).getByText("GET http://127.0.0.1:3004/ returned 502").tagName).toBe("PRE");
    });
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(visibleToast()).toHaveTextContent("Deploy failed");
    vi.useRealTimers();
    await userEvent.click(within(visibleToast()).getByRole("button", { name: "Dismiss notification" }));
    await waitFor(() => {
      expect(document.querySelector(".toast")).toBeNull();
    });
  });

  it("offers an action that runs its handler", async () => {
    const onClick = vi.fn();
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.warning("Certificate expires in 6 days", { action: { label: "Renew now", onClick } });
    });
    await waitFor(() => {
      expect(visibleToast()).toHaveTextContent("Renew now");
    });
    await userEvent.click(within(visibleToast()).getByRole("button", { name: "Renew now" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("updates a toast in place when the id is reused, and says the new words once", async () => {
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.info("Backup started", { id: "backup" });
    });
    await waitFor(() => {
      expect(visibleToast()).toHaveTextContent("Backup started");
    });
    act(() => {
      toast.success("Backup finished", { id: "backup" });
    });
    await waitFor(() => {
      expect(visibleToast()).toHaveTextContent("Backup finished");
    });
    expect(document.querySelectorAll(".toast")).toHaveLength(1);
    expect(liveRegionsSaying("Backup finished")).toHaveLength(1);
    expect(screen.queryByText("Backup started")).not.toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.error("Deploy failed", { description: "The health check did not pass.", detail: "502" });
    });
    await waitFor(() => {
      expect(visibleToast()).toHaveTextContent("Deploy failed");
    });
    await expectNoAxeViolations(document.body);
  });
});

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import { ToastProvider } from "./Toast";
import { toast } from "./toast";

/** The rendered toast, as opposed to the copy Base UI puts in its announcement region. */
function visibleToast(): HTMLElement {
  const element = document.querySelector<HTMLElement>(".toast");
  if (!element) throw new Error("No toast is rendered");
  return element;
}

afterEach(() => {
  act(() => {
    toast.dismiss();
  });
});

describe("Toast", () => {
  it("announces the outcome through a live region", async () => {
    render(<ToastProvider>{null}</ToastProvider>);
    act(() => {
      toast.error("Deploy failed");
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Deploy failed");
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
    // Base UI hides the visual toast from assistive technology (the live region speaks for
    // it) until focus enters the region, so the button is found by its label attribute.
    await userEvent.click(within(visibleToast()).getByLabelText("Dismiss notification"));
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
    await userEvent.click(within(visibleToast()).getByText("Renew now"));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("updates a toast in place when the id is reused", async () => {
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

import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../../test/axe";
import { renderConsole } from "../../../test/console";
import { FakeWebSocket, fakeBackend, json } from "../../../test/fakes";
import { TAB_DOMAIN, appRoutes } from "../testRoutes";
import { journalLine } from "./LogsTab";

async function logsAt(app: Record<string, unknown> = {}) {
  vi.stubGlobal("WebSocket", FakeWebSocket);
  // The virtualizer sizes its window from offsetHeight, which jsdom reports as 0.
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockReturnValue(400);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(800);
  const backend = fakeBackend(appRoutes(app, { "POST /api/auth/ws-ticket": () => json(200, { ticket: "t1" }) }));
  const harness = renderConsole(`/apps/${TAB_DOMAIN}/logs`);
  await screen.findByRole("heading", { level: 1, name: TAB_DOMAIN });
  return { ...harness, backend };
}

async function socket(): Promise<FakeWebSocket> {
  await waitFor(() => {
    expect(FakeWebSocket.instances.some((candidate) => candidate.url.includes(`/ws/logs/${TAB_DOMAIN}`))).toBe(true);
  });
  const found = FakeWebSocket.instances.find((candidate) => candidate.url.includes(`/ws/logs/${TAB_DOMAIN}`));
  if (!found) throw new Error("no log socket");
  return found;
}

describe("a journal line", () => {
  it("moves systemd's time to the time column, drops the host, and keeps the rest verbatim", () => {
    expect(journalLine({ id: 1, text: "2026-09-25T21:45:52+0100 web-01 shop-example-com[41234]: GET / 200 in 38ms" })).toEqual({
      id: 1,
      ts: "21:45:52",
      text: "shop-example-com[41234]: GET / 200 in 38ms",
    });
  });

  it("leaves a line in another form as it is", () => {
    const line = { id: 2, text: "-- Boot 4f1c --" };
    expect(journalLine(line)).toBe(line);
  });
});

// Whole-console renders: generous under a loaded machine or a slow CI runner.
describe("the logs tab", { timeout: 20_000 }, () => {
  it("follows the journal live, marks failures, and says when it is reconnecting", async () => {
    await logsAt();
    const ws = await socket();
    expect(ws.url).toContain("lines=200");
    act(() => {
      ws.open();
      ws.frame({ type: "connected", domain: TAB_DOMAIN, service: "shop-example-com" });
      ws.frame({ type: "log", data: "2026-09-25T21:45:52+0100 web-01 shop-example-com[41234]: GET / 200 in 38ms" });
      ws.frame({ type: "log", data: "2026-09-25T21:45:53+0100 web-01 shop-example-com[41234]: Error: connect ECONNREFUSED 127.0.0.1:6379" });
    });
    const journal = await screen.findByRole("region", { name: `Journal of ${TAB_DOMAIN}` });
    expect(await within(journal).findByText(/GET \/ 200 in 38ms/)).toBeInTheDocument();
    expect(within(journal).getByText(/ECONNREFUSED/).closest("[data-index]")).toHaveClass("bg-fail-soft");
    expect(screen.getByText("2 lines")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();

    act(() => {
      ws.drop();
    });
    expect(await screen.findByText("Reconnecting")).toBeInTheDocument();
  });

  it("shows the stream's own error verbatim", async () => {
    await logsAt();
    const ws = await socket();
    act(() => {
      ws.open();
      ws.frame({ type: "error", message: "journalctl not found. Log streaming requires systemd." });
    });
    expect(await screen.findByText("journalctl not found. Log streaming requires systemd.")).toBeInTheDocument();
    expect(screen.getByText("The journal stream failed")).toBeInTheDocument();
  });

  it("lets `/` search the journal", async () => {
    const { user } = await logsAt();
    await socket();
    const search = await screen.findByRole("searchbox", { name: "Search output" });
    await waitFor(() => {
      expect(search).toHaveAttribute("data-page-search");
    });
    await user.keyboard("/");
    expect(search).toHaveFocus();
  });

  it("does not open a stream for a static site, which has no unit", async () => {
    await logsAt({ status: "static", active: false, port: null });
    expect(await screen.findByRole("heading", { name: "A static site has no process to log" })).toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("has no accessibility violations", async () => {
    await logsAt();
    const ws = await socket();
    act(() => {
      ws.open();
      ws.frame({ type: "log", data: "2026-09-25T21:45:52+0100 web-01 shop-example-com[41234]: Ready in 412ms" });
    });
    await screen.findByText(/Ready in 412ms/);
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

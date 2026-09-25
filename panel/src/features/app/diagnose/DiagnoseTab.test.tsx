import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { expectNoAxeViolations } from "../../../test/axe";
import { renderConsole } from "../../../test/console";
import { fakeBackend, json, problem, signedInRoutes } from "../../../test/fakes";
import type { RouteHandler } from "../../../test/fakes";

const DOMAIN = "shop.example.com";

const DOWN = {
  domain: DOMAIN,
  verdict: "down",
  probable_cause: "The app listens on 3001, not the recorded port 3000.",
  checks: [
    { name: "unit", status: "ok", summary: "shop-example-com.service is active (running)", evidence: "ActiveState=active\nSubState=running" },
    {
      name: "port",
      status: "fail",
      summary: "The app listens on 3001, not the recorded port 3000",
      evidence: 'LISTEN 0 511 127.0.0.1:3001 0.0.0.0:* users:(("node",pid=4242,fd=19))',
    },
    { name: "journal", status: "warn", summary: "Last 2 journal line(s)", evidence: "Error: listen EADDRINUSE :::3000" },
    { name: "oom", status: "ok", summary: "No OOM kills in the kernel log in the last 7 days", evidence: "" },
    { name: "certificate", status: "skip", summary: "No certificate found for shop.example.com", evidence: "" },
  ],
};

const HEALTHY = {
  domain: DOMAIN,
  verdict: "healthy",
  probable_cause: null,
  checks: [{ name: "unit", status: "ok", summary: "shop-example-com.service is active (running)", evidence: "ActiveState=active" }],
};

function at(rows: readonly HTMLElement[], index: number): HTMLElement {
  const row = rows[index];
  if (!row) throw new Error(`No row ${String(index)}`);
  return row;
}

async function diagnoseTab(diagnose: RouteHandler) {
  const backend = fakeBackend({
    ...signedInRoutes(),
    "GET /api/certs": () => json(200, { certificates: [], total: 0 }),
    "GET /api/jobs/active": () => json(200, { jobs: [], total: 0, active: 0 }),
    [`GET /api/apps/${DOMAIN}/diagnose`]: diagnose,
  });
  const harness = renderConsole(`/apps/${DOMAIN}/diagnose`);
  await screen.findByRole("heading", { level: 1, name: DOMAIN });
  return { ...harness, backend };
}

describe("the Diagnose tab", () => {
  it("puts the verdict and the probable cause first", async () => {
    await diagnoseTab(() => json(200, DOWN));
    const verdict = await screen.findByRole("heading", { level: 2, name: "Verdict: Down" });
    expect(verdict.querySelector("[data-verdict]")).toHaveAttribute("data-verdict", "down");
    expect(screen.getByText("Probable cause")).toBeInTheDocument();
    expect(screen.getByText(DOWN.probable_cause)).toBeInTheDocument();
    expect(screen.getByText(/^5 checks:/)).toBeInTheDocument();
  });

  it("lists each check with its status as a word and its output verbatim", async () => {
    await diagnoseTab(() => json(200, DOWN));
    const checks = await screen.findByRole("region", { name: "Checks" });
    const rows = within(checks).getAllByRole("listitem");
    expect(rows).toHaveLength(5);
    expect(within(at(rows, 1)).getByText("Failed")).toBeInTheDocument();
    expect(within(at(rows, 1)).getByText("Listening port")).toBeInTheDocument();

    // What did not pass is open; its output is the system's, in mono.
    const evidence = within(at(rows, 1)).getByText(DOWN.checks[1]?.evidence ?? "");
    expect(evidence.tagName).toBe("PRE");
    expect(evidence).toBeVisible();
    expect(rows[1]?.querySelector("details")).toHaveAttribute("open");
    expect(rows[2]?.querySelector("details")).toHaveAttribute("open");
    // What passed stays folded; a probe with nothing to show says so.
    expect(rows[0]?.querySelector("details")).not.toHaveAttribute("open");
    expect(within(at(rows, 3)).getByText("No output")).toBeInTheDocument();
    expect(within(at(rows, 4)).getByText("Skipped")).toBeInTheDocument();
  });

  it("links a journal finding to the live log", async () => {
    await diagnoseTab(() => json(200, DOWN));
    const link = await screen.findByRole("link", { name: "Follow the live log" });
    expect(link).toHaveAttribute("href", `/apps/${DOMAIN}/logs`);
  });

  it("runs the probes again on request and announces the new verdict", async () => {
    let calls = 0;
    const { user, backend } = await diagnoseTab(() => {
      calls += 1;
      return json(200, calls === 1 ? DOWN : HEALTHY);
    });
    await screen.findByText(DOWN.probable_cause);
    await user.click(screen.getByRole("button", { name: "Run again" }));
    expect(await screen.findByRole("heading", { level: 2, name: "Verdict: Healthy" })).toBeInTheDocument();
    expect(screen.getByText("No single cause")).toBeInTheDocument();
    expect(backend.callsTo(`GET /api/apps/${DOMAIN}/diagnose`)).toHaveLength(2);
    await waitFor(() => {
      expect(screen.getByTestId("announcer-polite")).toHaveTextContent(`${DOMAIN}: Healthy.`);
    });
  });

  it("shows a failure to diagnose verbatim, with a way to try again", async () => {
    await diagnoseTab(() => problem(500, "internal", "ss: command not found", { hint: "Install iproute2." }));
    expect(await screen.findByText("Could not load the diagnosis of shop.example.com")).toBeInTheDocument();
    expect(screen.getByText("ss: command not found")).toBeInTheDocument();
    expect(screen.getByText("Install iproute2.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    await diagnoseTab(() => json(200, DOWN));
    await screen.findByText(DOWN.probable_cause);
    await expectNoAxeViolations(screen.getByRole("main"));
  });
});

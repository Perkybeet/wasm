import { describe, expect, it } from "vitest";

import { MACHINE } from "../../test/fakes";
import type { AppInfo, Deployment } from "../apps/data";
import { collectAttention } from "./attention";

function app(domain: string, status: string): AppInfo {
  return { domain, name: domain, status, active: status === "running", enabled: true, app_type: "nextjs", layout: "inplace" };
}

function deploy(id: number, domain: string, status: string, error: string | null = null): Deployment {
  return {
    id,
    domain,
    status,
    triggered_by: "panel",
    git_commit: "c07d5e3",
    git_branch: "main",
    started_at: "2026-09-25T19:20:35",
    finished_at: "2026-09-25T19:21:02",
    duration_s: 27,
    error,
    has_log: true,
  };
}

const HEALTHY = { ...MACHINE, units: { running: 3, failed: 0, stopped: 1 } };

describe("collectAttention", () => {
  it("is empty on a healthy machine", () => {
    expect(
      collectAttention({
        apps: [app("picconia.com", "running"), app("cittek.es", "stopped")],
        deployments: [deploy(2, "picconia.com", "success")],
        certificates: [],
        observations: [],
        machine: HEALTHY,
      }),
    ).toEqual([]);
  });

  it("names an app whose newest deploy failed, with the first line of the error verbatim", () => {
    const items = collectAttention({
      apps: [app("clientes.arennalabs.com", "stopped")],
      deployments: [
        deploy(12, "clientes.arennalabs.com", "failed", "npm ERR! code ELIFECYCLE\nnpm ERR! errno 1"),
        deploy(11, "clientes.arennalabs.com", "success"),
      ],
      machine: HEALTHY,
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      title: "clientes.arennalabs.com",
      severity: "fail",
      subject: { kind: "app", domain: "clientes.arennalabs.com" },
      reasons: [{ summary: "Last deploy failed", detail: "npm ERR! code ELIFECYCLE", deploymentId: 12 }],
    });
  });

  it("forgets a failure an app has since deployed over", () => {
    const items = collectAttention({
      apps: [app("picconia.com", "running")],
      deployments: [deploy(13, "picconia.com", "success"), deploy(12, "picconia.com", "failed", "boom")],
    });
    expect(items).toEqual([]);
  });

  it("ignores the history of an app that no longer exists", () => {
    expect(collectAttention({ apps: [], deployments: [deploy(5, "gone.example.com", "failed")] })).toEqual([]);
  });

  it("names apps in a problem state, from any of the backend's vocabularies", () => {
    const items = collectAttention({ apps: [app("a.example.com", "Restarting"), app("b.example.com", "failed")] });
    expect(items.map((item) => [item.title, item.severity])).toEqual([
      ["b.example.com", "fail"],
      ["a.example.com", "warn"],
    ]);
  });

  it("groups everything about one domain under it, with the worst severity", () => {
    const items = collectAttention({
      apps: [app("arennalabs.com", "running")],
      deployments: [deploy(3, "arennalabs.com", "rolled_back")],
      certificates: [{ domain: "arennalabs.com", domains: [], days_remaining: 12, expires_on: "2026-10-07", auto_renew: true }],
    });
    expect(items).toHaveLength(1);
    expect(items[0]?.severity).toBe("warn");
    expect(items[0]?.reasons.map((reason) => reason.summary)).toEqual([
      "Last deploy was rolled back",
      "Certificate expires in 12 days",
    ]);
  });

  it("warns 21 days before a certificate expires and fails once it has", () => {
    const items = collectAttention({
      apps: [],
      certificates: [
        { domain: "ok.example.com", domains: [], days_remaining: 21, auto_renew: true },
        { domain: "soon.example.com", domains: [], days_remaining: 1, auto_renew: true },
        { domain: "late.example.com", domains: [], days_remaining: -3, expires_on: "2026-09-22", auto_renew: true },
      ],
    });
    expect(items.map((item) => [item.title, item.severity, item.reasons[0]?.summary])).toEqual([
      ["late.example.com", "fail", "Certificate expired"],
      ["soon.example.com", "warn", "Certificate expires in 1 day"],
    ]);
    expect(items[1]?.subject).toEqual({ kind: "certificate", domain: "soon.example.com" });
  });

  it("reports failed units the apps' own state does not already name", () => {
    const machine = { ...MACHINE, units: { running: 3, failed: 2, stopped: 0 } };
    const unnamed = collectAttention({ apps: [app("x.example.com", "stopped")], machine });
    expect(unnamed.find((item) => item.subject.kind === "units")?.reasons[0]?.summary).toBe(
      "systemd reports 2 failed WASM units",
    );
    const named = collectAttention({ apps: [app("x.example.com", "failed"), app("y.example.com", "failed")], machine });
    expect(named.some((item) => item.subject.kind === "units")).toBe(false);
  });

  it("lists open monitor findings, not acknowledged ones", () => {
    const base = { pid: 4242, process_name: "xmrig", signal: "sustained high CPU", observed_at: "2026-09-25T18:00:00" };
    const items = collectAttention({
      observations: [
        { ...base, id: 1, severity: "warning", acknowledged: false },
        { ...base, id: 2, severity: "notice", acknowledged: true },
      ],
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ title: "xmrig", severity: "warn", reasons: [{ summary: "Monitor warning: sustained high CPU" }] });
  });
});

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";

import { certKeys } from "../../api/queries/certs";
import { siteKeys } from "../../api/queries/sites";
import { useServerEvents } from "../../realtime/events";
import { ANONYMOUS, FakeEventSource, fakeBackend, json } from "../../test/fakes";
import { useCertificateRefresh } from "./useCertificateJobs";

function mount(client: QueryClient) {
  fakeBackend({ "GET /api/auth/session": () => json(200, { ...ANONYMOUS, authenticated: true }) });
  return renderHook(
    () => {
      useServerEvents((url) => new FakeEventSource(url));
      useCertificateRefresh();
    },
    { wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children) },
  );
}

describe("useCertificateRefresh", () => {
  it("refreshes the certificate list, one certificate's own entry, and the sites once a certificate job ends", () => {
    const client = new QueryClient();
    client.setQueryData(certKeys.all, { certificates: [] });
    client.setQueryData(certKeys.detail("shop.example.com"), { domain: "shop.example.com" });
    client.setQueryData(siteKeys.all, { sites: [] });
    client.setQueryData(siteKeys.detail("shop.example.com"), { domain: "shop.example.com" });
    mount(client);

    FakeEventSource.latest().emit("job", { id: "j1", type: "cert_renew", status: "running", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(certKeys.all)?.isInvalidated).toBe(false);

    FakeEventSource.latest().emit("job", { id: "j1", type: "cert_renew", status: "completed", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(certKeys.all)?.isInvalidated).toBe(true);
    expect(client.getQueryState(certKeys.detail("shop.example.com"))?.isInvalidated).toBe(true);
    expect(client.getQueryState(siteKeys.all)?.isInvalidated).toBe(true);
    expect(client.getQueryState(siteKeys.detail("shop.example.com"))?.isInvalidated).toBe(true);
  });

  it("ignores jobs that are not about a certificate", () => {
    const client = new QueryClient();
    client.setQueryData(certKeys.all, { certificates: [] });
    mount(client);

    FakeEventSource.latest().emit("job", { id: "j2", type: "deploy", status: "completed", metadata: { domain: "shop.example.com" } });
    expect(client.getQueryState(certKeys.all)?.isInvalidated).toBe(false);
  });
});

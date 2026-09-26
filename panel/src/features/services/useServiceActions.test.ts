import { act, renderHook } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { createQueryClient } from "../../app/App";
import { serviceKeys } from "../../api/queries/services";
import { fakeBackend, json } from "../../test/fakes";
import { useServiceActions } from "./useServiceActions";

function wrapper(client = createQueryClient()) {
  return { client, Wrapper: ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children) };
}

describe("useServiceActions", () => {
  it("drops the deleted unit's cached detail (and its logs and config) rather than leaving it stale", async () => {
    fakeBackend({
      "DELETE /api/services/wasm-shop": () => json(200, { success: true, message: "Deleted wasm-shop" }),
    });
    const { client, Wrapper } = wrapper();
    client.setQueryData(serviceKeys.detail("wasm-shop"), { name: "wasm-shop", active: true });
    client.setQueryData(serviceKeys.config("wasm-shop"), { config: "[Unit]" });

    const { result } = renderHook(() => useServiceActions("wasm-shop"), { wrapper: Wrapper });
    await act(async () => {
      await result.current.remove.mutateAsync();
    });

    expect(client.getQueryData(serviceKeys.detail("wasm-shop"))).toBeUndefined();
    expect(client.getQueryData(serviceKeys.config("wasm-shop"))).toBeUndefined();
  });
});

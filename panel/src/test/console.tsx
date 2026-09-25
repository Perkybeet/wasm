import { createMemoryHistory } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { App, createQueryClient } from "../app/App";
import { buildRouter } from "../app/router";
import { installSessionHandling } from "../features/auth/session";
import { FakeEventSource } from "./fakes";

/**
 * Renders the whole console, router and all, at `path` on a memory history. The API must be
 * faked first (fakeBackend); `/events` is a FakeEventSource the test can drive.
 */
export function renderConsole(path: string) {
  vi.stubGlobal("EventSource", FakeEventSource);
  const queryClient = createQueryClient();
  // A failing request in a test is the answer under test, not something to retry.
  queryClient.setDefaultOptions({ queries: { retry: false, staleTime: 10_000 } });
  const history = createMemoryHistory({ initialEntries: [path] });
  const router = buildRouter(queryClient, history);
  const uninstall = installSessionHandling(router, queryClient);
  const user = userEvent.setup();
  const view = render(<App router={router} queryClient={queryClient} />);
  return {
    ...view,
    router,
    queryClient,
    user,
    history,
    cleanup: uninstall,
    location: () => router.state.location,
  };
}

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

// jsdom implements neither of these; Base UI, uPlot and the log viewer read them. Plain
// functions, not mocks, so that restoreMocks between tests cannot strip them.
const noop = (): void => undefined;

if (typeof window.matchMedia !== "function") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string): MediaQueryList =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: noop,
        removeEventListener: noop,
        addListener: noop,
        removeListener: noop,
        dispatchEvent: () => false,
      }) as MediaQueryList,
  });
}

class ResizeObserverStub {
  observe = noop;
  unobserve = noop;
  disconnect = noop;
}
if (typeof window.ResizeObserver !== "function") {
  Object.defineProperty(window, "ResizeObserver", { writable: true, value: ResizeObserverStub });
}

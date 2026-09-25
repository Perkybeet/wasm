import { api } from "../api/client";

declare global {
  interface Window {
    /** Development only: drive the real client from the browser console or a script. */
    __wasmDev?: { api: typeof api };
  }
}

/**
 * Exposes the API client on `window.__wasmDev` in development, so a review script can fire a
 * request that walks the real error paths (an elevation, an expired session). Imported behind
 * import.meta.env.DEV only; a production build contains none of it.
 */
export function exposeDevTools(): void {
  window.__wasmDev = { api };
}

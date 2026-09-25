import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import type { ProxyOptions } from "vite";

import { mockBackend } from "./mock/backend.ts";

// The console talks to the real FastAPI backend in development. Point VITE_BACKEND at any
// running `wasm web` instance; the default is the panel's own default bind address.
const DEFAULT_BACKEND = "http://127.0.0.1:8080";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const backend = env.VITE_BACKEND ?? DEFAULT_BACKEND;
  // VITE_MOCK=1 answers the API from mock/backend.ts inside the dev server, for reviewing the
  // console without a Python backend. Serve-only: `vite build` never includes it.
  const mock = env.VITE_MOCK === "1";
  const proxy: Record<string, ProxyOptions> = mock
    ? {}
    : {
        "/api": { target: backend, changeOrigin: false },
        "/events": { target: backend, changeOrigin: false },
        "/hooks": { target: backend, changeOrigin: false },
        "/ws": { target: backend, ws: true, changeOrigin: false },
      };

  return {
    // The router plugin must run before the React plugin so that it can split route files.
    plugins: [
      tanstackRouter({ target: "react", autoCodeSplitting: true, quoteStyle: "double" }),
      react(),
      tailwindcss(),
      ...(mock ? [mockBackend()] : []),
    ],
    base: "/",
    build: {
      // The build is committed: OBS packages from `git archive HEAD` with no network and
      // never runs Node, so the Python package has to carry the console ready to serve.
      // CI rebuilds and fails when this directory differs from what the source produces.
      outDir: "../src/wasm/web/static",
      emptyOutDir: true,
      assetsDir: "assets",
      // Never inline an asset as a data: URI. Vite inlines anything under 4 KiB by default,
      // which turned the smallest font subset into data:font/woff2 - and the CSP's
      // `font-src 'self'` blocks that, so the glyphs it covers silently fell back.
      assetsInlineLimit: 0,
      sourcemap: false,
      // Rolldown (Vite 8) names chunks by content hash and has no parallel-file-ops or
      // CommonJS-plugin knobs; determinism is verified by building twice and diffing.
      reportCompressedSize: false,
    },
    server: {
      proxy,
    },
  };
});

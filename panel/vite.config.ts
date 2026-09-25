import tailwindcss from "@tailwindcss/vite";
import { tanstackRouter } from "@tanstack/router-plugin/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// The console talks to the real FastAPI backend in development. Point VITE_BACKEND at any
// running `wasm web` instance; the default is the panel's own default bind address.
const DEFAULT_BACKEND = "http://127.0.0.1:8080";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "VITE_");
  const backend = env.VITE_BACKEND ?? DEFAULT_BACKEND;

  return {
    // The router plugin must run before the React plugin so that it can split route files.
    plugins: [
      tanstackRouter({ target: "react", autoCodeSplitting: true, quoteStyle: "double" }),
      react(),
      tailwindcss(),
    ],
    base: "/",
    build: {
      // Until the server cut-over (Task 2.1, later step) the legacy panel still owns
      // src/wasm/web/static, so the console builds into a gitignored directory.
      outDir: "dist",
      emptyOutDir: true,
      assetsDir: "assets",
      sourcemap: false,
      // Rolldown (Vite 8) names chunks by content hash and has no parallel-file-ops or
      // CommonJS-plugin knobs; determinism is verified by building twice and diffing.
      reportCompressedSize: false,
    },
    server: {
      proxy: {
        "/api": { target: backend, changeOrigin: false },
        "/events": { target: backend, changeOrigin: false },
        "/hooks": { target: backend, changeOrigin: false },
        "/ws": { target: backend, ws: true, changeOrigin: false },
      },
    },
  };
});

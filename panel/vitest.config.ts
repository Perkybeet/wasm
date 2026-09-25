import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // Stylesheets are not applied in jsdom, but tokens.css is read as text by the contrast test.
    css: { include: [/tokens\.css/] },
    restoreMocks: true,
  },
});

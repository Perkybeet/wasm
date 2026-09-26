import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // Stylesheets are not applied in jsdom, but tokens.css and fonts.css are read as text by
    // the tokens test (contrast, font stacks and their fallback faces).
    css: { include: [/tokens\.css/, /fonts\.css/] },
    restoreMocks: true,
  },
});

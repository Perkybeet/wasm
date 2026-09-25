import "@fontsource-variable/mona-sans/wdth.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
import "./styles/app.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App, createQueryClient } from "./app/App";
import { createAppRouter } from "./app/router";
import { initTheme } from "./app/theme";
import { installSessionHandling } from "./features/auth/session";

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html must contain <div id=\"root\"> for the console to mount.");
}

// Before the first render, so a pinned theme never flashes the other one.
initTheme();

const queryClient = createQueryClient();
const router = await createAppRouter(queryClient);
installSessionHandling(router, queryClient);

if (import.meta.env.DEV) {
  const { exposeDevTools } = await import("./dev/devtools");
  exposeDevTools();
}

createRoot(container).render(
  <StrictMode>
    <App router={router} queryClient={queryClient} />
  </StrictMode>,
);

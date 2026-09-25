import "@fontsource-variable/mona-sans/wdth.css";
import "@fontsource-variable/jetbrains-mono/wght.css";
import "./styles/app.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import { createAppRouter } from "./app/router";

const container = document.getElementById("root");
if (!container) {
  throw new Error("index.html must contain <div id=\"root\"> for the console to mount.");
}

const router = await createAppRouter();

createRoot(container).render(
  <StrictMode>
    <App router={router} />
  </StrictMode>,
);

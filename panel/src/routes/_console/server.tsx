import { createFileRoute } from "@tanstack/react-router";

import { ServerPage } from "../../features/server/ServerPage";

export const Route = createFileRoute("/_console/server")({
  component: ServerPage,
});

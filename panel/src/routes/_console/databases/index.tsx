import { createFileRoute } from "@tanstack/react-router";

import { DatabasesPage } from "../../../features/databases/DatabasesPage";

export const Route = createFileRoute("/_console/databases/")({
  component: DatabasesPage,
});

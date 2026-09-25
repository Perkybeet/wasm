import { createFileRoute } from "@tanstack/react-router";

import { NewAppWizard } from "../../../features/new-app/NewAppWizard";

/** The new-app wizard: source, review, deploy. */
export const Route = createFileRoute("/_console/apps/new")({
  component: NewAppWizard,
});

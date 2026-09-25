import { createFileRoute } from "@tanstack/react-router";

import { DiagnoseTab } from "../../../../features/app/diagnose/DiagnoseTab";

export const Route = createFileRoute("/_console/apps/$domain/diagnose")({
  component: AppDiagnoseTab,
});

function AppDiagnoseTab() {
  const { domain } = Route.useParams();
  return <DiagnoseTab domain={domain} />;
}

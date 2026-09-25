import { createFileRoute } from "@tanstack/react-router";
import { Stethoscope } from "lucide-react";

import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/$domain/diagnose")({
  component: AppDiagnoseTab,
});

function AppDiagnoseTab() {
  const { domain } = Route.useParams();
  return (
    <Placeholder
      icon={<Stethoscope />}
      title="Why this app is down"
      description="The unit, its recent journal lines, the web server error log, the listening port, the certificate and the last deploy, checked together with the probable cause first."
      command={`wasm status ${domain}`}
      documentTitle={`Diagnose - ${domain}`}
    />
  );
}

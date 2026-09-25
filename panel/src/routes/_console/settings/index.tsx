import { createFileRoute } from "@tanstack/react-router";
import { Settings2 } from "lucide-react";

import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/settings/")({
  component: GeneralSettings,
});

function GeneralSettings() {
  return (
    <Placeholder
      icon={<Settings2 />}
      title="How WASM runs here"
      description="The apps directory, the web server, the email for certificates and how long backups are kept."
      command="wasm config show"
      documentTitle="General settings"
    />
  );
}

import { createFileRoute } from "@tanstack/react-router";
import { Info } from "lucide-react";

import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/settings/about")({
  component: AboutSettings,
});

function AboutSettings() {
  return (
    <Placeholder
      icon={<Info />}
      title="Version and updates"
      description="The installed version of WASM and whether a newer one is available."
      command="wasm --version"
      documentTitle="About settings"
    />
  );
}

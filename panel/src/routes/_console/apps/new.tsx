import { createFileRoute } from "@tanstack/react-router";
import { Rocket } from "lucide-react";

import { PageHeader } from "../../../app/PageHeader";
import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/apps/new")({
  component: NewApplicationPage,
});

function NewApplicationPage() {
  return (
    <>
      <PageHeader
        title="New application"
        description="Deploy from a git repository or a directory on this server."
        breadcrumbs={[{ label: "Applications", to: "/apps" }]}
      />
      <Placeholder
        icon={<Rocket />}
        title="Point at a repository to begin"
        description="WASM clones it and proposes the type, build and start commands, port and the variables from .env.example. Everything stays editable before the first deploy, and the build log streams live."
        command="wasm create"
      />
    </>
  );
}

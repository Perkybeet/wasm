import { Link, createFileRoute } from "@tanstack/react-router";
import { Boxes, Plus } from "lucide-react";

import { PageHeader } from "../../../app/PageHeader";
import { Placeholder } from "../../../app/Placeholder";
import { buttonClassName } from "../../../components/ui/Button";

export const Route = createFileRoute("/_console/apps/")({
  component: ApplicationsPage,
});

function ApplicationsPage() {
  return (
    <>
      <PageHeader
        title="Applications"
        description="Every app deployed on this machine, its state and its last deploy."
      />
      <Placeholder
        icon={<Boxes />}
        title="Your applications, in one table"
        description="Search and filter by state and type, and open, restart or update any app from its row."
        command="wasm list"
        action={
          <Link to="/apps/new" className={buttonClassName("primary")}>
            <Plus aria-hidden="true" />
            New application
          </Link>
        }
      />
    </>
  );
}

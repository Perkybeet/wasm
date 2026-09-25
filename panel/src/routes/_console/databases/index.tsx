import { createFileRoute } from "@tanstack/react-router";
import { Database } from "lucide-react";

import { PageHeader } from "../../../app/PageHeader";
import { Placeholder } from "../../../app/Placeholder";

export const Route = createFileRoute("/_console/databases/")({
  component: DatabasesPage,
});

function DatabasesPage() {
  return (
    <>
      <PageHeader
        title="Databases"
        description="Database engines on this machine, their databases, users and backups."
      />
      <Placeholder
        icon={<Database />}
        title="Engines, databases and users"
        description="Install and control each engine, create databases and users, grant access, back them up and open a SQL console."
        command="wasm db list"
      />
    </>
  );
}

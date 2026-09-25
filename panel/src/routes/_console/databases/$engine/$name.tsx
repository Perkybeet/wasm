import { createFileRoute } from "@tanstack/react-router";
import { SquareTerminal } from "lucide-react";

import { PageHeader } from "../../../../app/PageHeader";
import { Placeholder } from "../../../../app/Placeholder";

export const Route = createFileRoute("/_console/databases/$engine/$name")({
  component: DatabasePage,
});

function DatabasePage() {
  const { engine, name } = Route.useParams();
  return (
    <>
      <PageHeader
        title={name}
        description={`A ${engine} database on this machine.`}
        breadcrumbs={[{ label: "Databases", to: "/databases" }]}
      />
      <Placeholder
        icon={<SquareTerminal />}
        title="SQL console"
        description="Run read queries and see the result as a table with column types, row count and duration. Writes need a fresh confirmation."
        command={`wasm db info ${name}`}
      />
    </>
  );
}

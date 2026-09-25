import { createFileRoute } from "@tanstack/react-router";

import { DatabasePage } from "../../../../features/databases/DatabasePage";

export const Route = createFileRoute("/_console/databases/$engine/$name")({
  component: DatabaseRoute,
});

function DatabaseRoute() {
  const { engine, name } = Route.useParams();
  return <DatabasePage engine={engine} name={name} />;
}

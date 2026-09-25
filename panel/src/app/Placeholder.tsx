import type { ReactNode } from "react";

import { EmptyState } from "../components/ui/EmptyState";
import { useDocumentTitle } from "./documentTitle";

export interface PlaceholderProps {
  /** A lucide icon element. */
  icon: ReactNode;
  /** What this place will show, in the operator's words. */
  title: string;
  description: ReactNode;
  /** The CLI command that shows the same thing today. */
  command?: string;
  action?: ReactNode;
  /** Title of the browser tab, for views nested in a layout that owns the page header. */
  documentTitle?: string;
}

/**
 * What a page shows until its feature lands: the real purpose of the page and the command
 * that does the same from a terminal, so the route is useful rather than a dead end.
 */
export function Placeholder({ icon, title, description, command, action, documentTitle }: PlaceholderProps) {
  useDocumentTitle(documentTitle ?? null, 1);
  return (
    <EmptyState
      level={2}
      icon={icon}
      title={title}
      description={description}
      {...(command !== undefined ? { command } : {})}
      {...(action !== undefined ? { action } : {})}
      className="py-16"
    />
  );
}

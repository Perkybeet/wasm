import type { ReactNode } from "react";

import { Button } from "../../components/ui/Button";

export interface RowActionProps {
  /** The full accessible name, naming the row: "Revoke ci-deploy". Starts with `text`. */
  label: string;
  /** The words shown beside the icon: "Revoke". */
  text: string;
  icon: ReactNode;
  onClick: () => void;
  loading?: boolean;
}

/**
 * A table row's one action. On a phone only its icon shows, so the column fits the screen;
 * it is the same single control at every size, named in full for assistive technology.
 */
export function RowAction({ label, text, icon, onClick, loading = false }: RowActionProps) {
  return (
    <Button size="sm" variant="ghost" aria-label={label} icon={icon} loading={loading} onClick={onClick}>
      <span className="max-sm:sr-only">{text}</span>
    </Button>
  );
}

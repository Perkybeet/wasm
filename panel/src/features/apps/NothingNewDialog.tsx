import type { ApiError } from "../../api/errors";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";

export interface NothingNewDialogProps {
  domain: string;
  /** The `409 nothing_new` the update was answered with; the dialog is open while it is set. */
  refusal: ApiError | null;
  /** The forced retry is in flight. */
  pending: boolean;
  /** Retries the update with `force`. */
  onRebuild: () => void;
  onClose: () => void;
}

/**
 * The question an update without anything new asks: the branch head is the commit that is
 * live, so updating would rebuild the same commit. The backend's own sentence says which
 * commit and which branch, and its hint when rebuilding still makes sense; nothing is
 * paraphrased.
 */
export function NothingNewDialog({ domain, refusal, pending, onRebuild, onClose }: NothingNewDialogProps) {
  return (
    <Dialog
      open={refusal !== null}
      onOpenChange={(open) => {
        if (!open && !pending) onClose();
      }}
      size="sm"
      title={`Nothing new to deploy to ${domain}`}
      description={refusal?.detail}
      footer={
        <>
          <Button disabled={pending} onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={pending} onClick={onRebuild}>
            Rebuild anyway
          </Button>
        </>
      }
    >
      {refusal?.hint ? <p className="text-13 text-pretty text-fg-muted">{refusal.hint}</p> : null}
    </Dialog>
  );
}

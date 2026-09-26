import { AlertDialog } from "@base-ui/react/alert-dialog";
import { useId, useRef, useState } from "react";
import type { ReactElement, ReactNode, SyntheticEvent } from "react";

import { cx } from "../../lib/cx";
import { describeError } from "../../lib/errors";
import { Button } from "./Button";
import { BACKDROP, DialogFrame, MODAL_POPUP, MODAL_VIEWPORT } from "./Dialog";
import { Input } from "./Input";
import { SystemOutput } from "./SystemOutput";

export interface ConfirmDialogProps {
  title: string;
  /** What will happen, concretely: what is stopped, deleted, kept. */
  description: ReactNode;
  /** The exact text the operator must type, usually the resource name. */
  confirmText: string;
  /** The verb on the action button: "Delete application", not "Confirm". */
  actionLabel: string;
  destructive?: boolean;
  /** Runs the action. The dialog stays open, busy, until it settles; a rejection is shown verbatim. */
  onConfirm: () => Promise<void>;
  trigger?: ReactElement<Record<string, unknown>>;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

/**
 * Confirmation for an irreversible action. The action stays disabled until the resource name
 * is typed exactly, which makes the operator read what they are about to destroy.
 */
export function ConfirmDialog({
  title,
  description,
  confirmText,
  actionLabel,
  destructive = true,
  onConfirm,
  trigger,
  open,
  onOpenChange,
}: ConfirmDialogProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<{ hint: string | null; detail: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();

  const isOpen = open ?? internalOpen;
  const matches = typed === confirmText;

  const setOpen = (next: boolean): void => {
    // A running action cannot be abandoned half-way from the keyboard or backdrop.
    if (!next && pending) return;
    if (open === undefined) setInternalOpen(next);
    onOpenChange?.(next);
    if (!next) {
      setTyped("");
      setFailure(null);
    }
  };

  const submit = async (event: SyntheticEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (!matches || pending) return;
    setPending(true);
    setFailure(null);
    try {
      await onConfirm();
      setPending(false);
      if (open === undefined) setInternalOpen(false);
      onOpenChange?.(false);
      setTyped("");
    } catch (error: unknown) {
      setPending(false);
      setFailure(describeError(error));
    }
  };

  return (
    <AlertDialog.Root open={isOpen} onOpenChange={(next: boolean) => setOpen(next)}>
      {trigger !== undefined ? <AlertDialog.Trigger render={trigger} /> : null}
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className={BACKDROP} />
        <AlertDialog.Viewport className={MODAL_VIEWPORT}>
          <AlertDialog.Popup initialFocus={inputRef} className={cx(MODAL_POPUP, "sm:max-w-[440px]")}>
            <form onSubmit={(event) => void submit(event)} className="contents">
              <DialogFrame
                title={title}
                description={description}
                Title={AlertDialog.Title}
                Description={AlertDialog.Description}
                footer={
                  <>
                    <AlertDialog.Close render={<Button disabled={pending}>Cancel</Button>} />
                    <Button
                      type="submit"
                      variant={destructive ? "danger" : "primary"}
                      disabled={!matches}
                      loading={pending}
                    >
                      {actionLabel}
                    </Button>
                  </>
                }
              >
                <div className="flex flex-col gap-1.5">
                  <label htmlFor={inputId} className="text-13 text-fg-muted">
                    Type{" "}
                    <span translate="no" className="mono rounded-[4px] bg-bg-sunken px-1 py-0.5 text-fg select-all">
                      {confirmText}
                    </span>{" "}
                    to confirm
                  </label>
                  <Input
                    id={inputId}
                    ref={inputRef}
                    mono
                    value={typed}
                    onValueChange={(value: string) => setTyped(value)}
                    autoComplete="off"
                    autoCapitalize="off"
                    spellCheck={false}
                    disabled={pending}
                  />
                </div>
                {failure !== null ? (
                  <div role="alert" className="mt-4 flex flex-col gap-2 rounded-control border border-fail/30 bg-fail-soft p-3">
                    <p className="text-13 font-medium text-fail">{failure.hint ?? "The action failed. The system said:"}</p>
                    <SystemOutput label="What the system said" maxHeight="max-h-40">
                      {failure.detail}
                    </SystemOutput>
                  </div>
                ) : null}
              </DialogFrame>
            </form>
          </AlertDialog.Popup>
        </AlertDialog.Viewport>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}


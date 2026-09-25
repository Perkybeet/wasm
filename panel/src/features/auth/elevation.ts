/**
 * The pending "Confirm it's you" request, outside React.
 *
 * The API client calls `elevate()` when an action answers 403 elevation_required and waits
 * on the promise before retrying; <ElevateDialog> renders whatever request is pending and
 * settles it. Concurrent refusals share one request, so the operator confirms once.
 */

import { useSyncExternalStore } from "react";

import { ElevationCancelledError } from "../../api/errors";

interface PendingRequest {
  promise: Promise<void>;
  resolve: () => void;
  reject: (error: Error) => void;
}

let pending: PendingRequest | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

/** Asks the operator to confirm it's them. Resolves when they do; rejects when they cancel. */
export function elevate(): Promise<void> {
  if (pending) return pending.promise;
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  pending = { promise, resolve, reject };
  notify();
  return promise;
}

export function resolveElevation(): void {
  const request = pending;
  pending = null;
  request?.resolve();
  notify();
}

/** Declines the pending request: the action that asked for it fails with a clear error. */
export function cancelElevation(): void {
  const request = pending;
  pending = null;
  request?.reject(new ElevationCancelledError());
  notify();
}

export function useElevationRequested(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => pending !== null,
  );
}

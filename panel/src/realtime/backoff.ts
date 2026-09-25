/** How long to wait before reconnecting a live stream: 1s, doubling, never more than 30s. */
export const RECONNECT = { initialMs: 1_000, maxMs: 30_000 } as const;

/**
 * The delay before reconnect attempt `failures` (0 for the first retry). The sequence resets
 * once a connection succeeds, so a panel restart costs a second, not the accumulated wait.
 */
export function reconnectDelay(failures: number): number {
  return Math.min(RECONNECT.maxMs, RECONNECT.initialMs * 2 ** Math.max(0, failures));
}

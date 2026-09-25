/**
 * How the console writes numbers, sizes, durations and times. One implementation, so a
 * memory figure reads the same in the apps table, the app page and a chart axis.
 *
 * Numbers use en-US grouping (the interface is English); sizes are binary (1 KB = 1024 B),
 * the way `df -h`, `free -h` and systemd's MemoryMax count them.
 */

const NUMBER = new Intl.NumberFormat("en-US");
const COMPACT = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;

/** Few significant figures: two below 1, one decimal below 10, whole numbers above. */
function significant(value: number): string {
  const abs = Math.abs(value);
  const digits = abs > 0 && abs < 1 ? 2 : abs < 10 ? 1 : 0;
  return value.toFixed(digits);
}

/**
 * A size in bytes: "512 B", "1.5 KB", "96 MB", "6.2 GB". A unit is left for the next one up
 * once it would print four digits, so 1006 GB reads "0.98 TB".
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes)) return "-";
  const sign = bytes < 0 ? "-" : "";
  let value = Math.abs(bytes);
  let unit = 0;
  while (value >= 1000 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = unit === 0 ? String(Math.round(value)) : significant(value);
  return `${sign}${text} ${BYTE_UNITS[unit] ?? "B"}`;
}

/** A transfer rate: "1.2 MB/s". */
export function formatBytesRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`;
}

/**
 * A percentage already on the 0-100 scale: "5.7%", "32%". One decimal below ten, where the
 * decimal is the difference between idle and busy; whole numbers above, where it is noise.
 */
export function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return "-";
  const abs = Math.abs(value);
  const text = abs === 0 ? "0" : abs < 10 ? value.toFixed(1) : String(Math.round(value));
  return `${text}%`;
}

/** A count: "1,284" in full up to ten thousand, then compact ("12.9K", "4.2M"). */
export function formatCount(value: number): string {
  if (!Number.isFinite(value)) return "-";
  return Math.abs(value) < 10_000 ? NUMBER.format(value) : COMPACT.format(value);
}

/**
 * A span of time in seconds as the two largest units: "3 ms", "2.4s", "14s", "2m 05s",
 * "1h 12m", "3d 4h".
 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "-";
  if (seconds < 1) return `${String(Math.max(1, Math.round(seconds * 1000)))} ms`;
  if (seconds < 10) return `${(Math.floor(seconds * 10) / 10).toFixed(1)}s`;
  const whole = Math.floor(seconds);
  if (whole < 60) return `${String(whole)}s`;
  const days = Math.floor(whole / 86_400);
  const hours = Math.floor((whole % 86_400) / 3_600);
  const minutes = Math.floor((whole % 3_600) / 60);
  const secs = whole % 60;
  if (days > 0) return `${String(days)}d ${String(hours)}h`;
  if (hours > 0) return `${String(hours)}h ${String(minutes)}m`;
  return `${String(minutes)}m ${String(secs).padStart(2, "0")}s`;
}

// ---------------------------------------------------------------------------------------
// Time

/** "2026-09-25T19:20:35.378313", with or without an offset, microseconds allowed. */
const ISO = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$/;

/** systemd's own timestamps: "Fri 2026-09-25 13:06:35 UTC", "Fri 2026-09-25 13:06:35 +0200". */
const SYSTEMD = /^(?:[A-Z][a-z]{2} )?(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?: (UTC|GMT|[+-]\d{2}:?\d{2}))$/;

function offsetOf(zone: string): string {
  if (zone === "UTC" || zone === "GMT" || zone === "Z") return "Z";
  return zone.includes(":") ? zone : `${zone.slice(0, 3)}:${zone.slice(3)}`;
}

/**
 * Reads the timestamps the backend sends into a Date, or null when the text is not one the
 * console can place in time.
 *
 * - ISO 8601 with an offset is exact. Without one (the store writes naive local times) it
 *   is read as local time, which is right when the browser and the server share a zone.
 * - systemd prints its timestamps in the server's zone by abbreviation; only UTC/GMT and
 *   numeric offsets are unambiguous, so "CEST" and friends return null and the caller shows
 *   the text verbatim instead of guessing.
 * - Numbers are Unix time, in seconds below 10^12 and in milliseconds above.
 */
export function parseTimestamp(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    return new Date(value < 1e12 ? value * 1000 : value);
  }
  const text = value.trim();
  const iso = ISO.exec(text);
  if (iso) {
    const [, date, time, fraction, zone] = iso;
    // Engines disagree on more than three fractional digits; milliseconds are plenty.
    const millis = fraction === undefined ? "" : `.${fraction.slice(0, 3).padEnd(3, "0")}`;
    const parsed = new Date(`${date ?? ""}T${time ?? ""}${millis}${zone === undefined ? "" : offsetOf(zone)}`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  const systemd = SYSTEMD.exec(text);
  if (systemd) {
    const [, date, time, zone] = systemd;
    const parsed = new Date(`${date ?? ""}T${time ?? ""}${offsetOf(zone ?? "Z")}`);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  return null;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/**
 * How long ago (or how far ahead) a moment is, compactly: "just now", "42s ago", "3m ago",
 * "5h ago", "4d ago", then the date ("Sep 12", or "Sep 12, 2025" in another year).
 */
export function formatRelative(date: Date, now: Date = new Date()): string {
  const delta = Math.round((now.getTime() - date.getTime()) / 1000);
  const future = delta < 0;
  const seconds = Math.abs(delta);
  const say = (amount: number, unit: string): string =>
    future ? `in ${String(amount)}${unit}` : `${String(amount)}${unit} ago`;
  // Either side of now by a few seconds is the same moment: clocks and ticks drift that much.
  if (seconds < 10) return "just now";
  if (seconds < 60) return say(seconds, "s");
  if (seconds < 3_600) return say(Math.floor(seconds / 60), "m");
  if (seconds < 86_400) return say(Math.floor(seconds / 3_600), "h");
  if (seconds < 7 * 86_400) return say(Math.floor(seconds / 86_400), "d");
  const month = MONTHS[date.getMonth()] ?? "";
  const day = `${month} ${String(date.getDate())}`;
  return date.getFullYear() === now.getFullYear() ? day : `${day}, ${String(date.getFullYear())}`;
}

/** The zone's short name where the browser knows one ("WEST", "UTC", "GMT+2"). */
function zoneName(date: Date): string {
  const part = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" })
    .formatToParts(date)
    .find((p) => p.type === "timeZoneName");
  return part?.value ?? "";
}

/**
 * A moment in full, the way server logs print it: "2026-09-25 20:36:10 WEST". Unambiguous in
 * every locale and directly comparable with journal and nginx lines.
 */
export function formatDateTime(date: Date): string {
  const day = `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  const zone = zoneName(date);
  return zone === "" ? `${day} ${time}` : `${day} ${time} ${zone}`;
}

/**
 * Milliseconds until `formatRelative` of this moment next changes its text, so a live
 * label re-renders exactly when it would read differently and never in between.
 */
export function relativeRefreshMs(date: Date, now: Date = new Date()): number {
  const age = Math.abs(now.getTime() - date.getTime());
  if (age < 60_000) return 1_000;
  if (age < 3_600_000) return 30_000;
  if (age < 86_400_000) return 5 * 60_000;
  return 60 * 60_000;
}

import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatBytesRate,
  formatCount,
  formatDateTime,
  formatDuration,
  formatPercent,
  formatRelative,
  parseTimestamp,
  relativeRefreshMs,
} from "./format";

describe("formatBytes", () => {
  it.each([
    [0, "0 B"],
    [512, "512 B"],
    [999, "999 B"],
    [1_000, "0.98 KB"],
    [1_536, "1.5 KB"],
    [96 * 1024 * 1024, "96 MB"],
    [6_613_762_048, "6.2 GB"],
    [243_682_394_112, "227 GB"],
    [1_081_101_176_832, "0.98 TB"],
  ])("%d bytes read as %s", (bytes, text) => {
    expect(formatBytes(bytes)).toBe(text);
  });

  it("keeps the sign of a negative delta", () => {
    expect(formatBytes(-2048)).toBe("-2.0 KB");
  });

  it("prints a dash for a value that is not a number", () => {
    expect(formatBytes(Number.NaN)).toBe("-");
  });

  it("adds the per-second unit for rates", () => {
    expect(formatBytesRate(1_258_291)).toBe("1.2 MB/s");
  });
});

describe("formatPercent", () => {
  it.each([
    [0, "0%"],
    [5.7, "5.7%"],
    [9.94, "9.9%"],
    [31.5, "32%"],
    [100, "100%"],
  ])("%d reads as %s", (value, text) => {
    expect(formatPercent(value)).toBe(text);
  });
});

describe("formatCount", () => {
  it("groups thousands in full up to ten thousand", () => {
    expect(formatCount(1284)).toBe("1,284");
  });

  it("goes compact above", () => {
    expect(formatCount(12_900)).toBe("12.9K");
    expect(formatCount(4_200_000)).toBe("4.2M");
  });
});

describe("formatDuration", () => {
  it.each([
    [0.003205, "3 ms"],
    [0.0001, "1 ms"],
    [2.46, "2.4s"],
    [14.9, "14s"],
    [125, "2m 05s"],
    [4_320, "1h 12m"],
    [273_600, "3d 4h"],
  ])("%d seconds read as %s", (seconds, text) => {
    expect(formatDuration(seconds)).toBe(text);
  });

  it("refuses a negative span", () => {
    expect(formatDuration(-1)).toBe("-");
  });
});

describe("parseTimestamp", () => {
  it("reads an ISO time with an offset exactly", () => {
    expect(parseTimestamp("2026-09-25T19:20:35+02:00")?.toISOString()).toBe("2026-09-25T17:20:35.000Z");
    expect(parseTimestamp("2026-09-25T19:20:35Z")?.toISOString()).toBe("2026-09-25T19:20:35.000Z");
  });

  it("reads a naive store time as local time, microseconds and all", () => {
    const parsed = parseTimestamp("2026-09-25T19:20:35.378313");
    expect(parsed).not.toBeNull();
    expect(parsed?.getHours()).toBe(19);
    expect(parsed?.getMinutes()).toBe(20);
    expect(parsed?.getMilliseconds()).toBe(378);
  });

  it("reads systemd's timestamps when the zone is unambiguous", () => {
    expect(parseTimestamp("Fri 2026-09-25 13:06:35 UTC")?.toISOString()).toBe("2026-09-25T13:06:35.000Z");
    expect(parseTimestamp("Fri 2026-09-25 13:06:35 +0200")?.toISOString()).toBe("2026-09-25T11:06:35.000Z");
  });

  it("will not guess a zone abbreviation", () => {
    expect(parseTimestamp("Fri 2026-09-25 13:06:35 CEST")).toBeNull();
  });

  it("reads Unix time in seconds or milliseconds", () => {
    expect(parseTimestamp(1_790_360_435)?.toISOString()).toBe("2026-09-25T18:20:35.000Z");
    expect(parseTimestamp(1_790_360_435_000)?.toISOString()).toBe("2026-09-25T18:20:35.000Z");
  });

  it("returns null for nothing or nonsense", () => {
    expect(parseTimestamp(null)).toBeNull();
    expect(parseTimestamp(undefined)).toBeNull();
    expect(parseTimestamp("")).toBeNull();
    expect(parseTimestamp("yesterday")).toBeNull();
  });
});

describe("formatRelative", () => {
  const now = new Date(2026, 8, 25, 12, 0, 0);
  const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000);

  it.each([
    [3, "just now"],
    [42, "42s ago"],
    [180, "3m ago"],
    [5 * 3600, "5h ago"],
    [4 * 86_400, "4d ago"],
  ])("%d seconds ago reads %s", (seconds, text) => {
    expect(formatRelative(ago(seconds), now)).toBe(text);
  });

  it("names the day after a week, and the year only when it differs", () => {
    expect(formatRelative(new Date(2026, 8, 12, 9), now)).toBe("Sep 12");
    expect(formatRelative(new Date(2025, 8, 12, 9), now)).toBe("Sep 12, 2025");
  });

  it("speaks of the future ahead", () => {
    expect(formatRelative(new Date(now.getTime() + 3 * 3600 * 1000), now)).toBe("in 3h");
    expect(formatRelative(new Date(now.getTime() + 2000), now)).toBe("just now");
  });
});

describe("formatDateTime", () => {
  it("prints the moment the way logs do", () => {
    expect(formatDateTime(new Date(2026, 8, 5, 7, 3, 9))).toMatch(/^2026-09-05 07:03:09( \S+)?$/);
  });
});

describe("relativeRefreshMs", () => {
  const now = new Date(2026, 8, 25, 12);
  it("refreshes each second while seconds are shown, then less and less often", () => {
    expect(relativeRefreshMs(new Date(now.getTime() - 30_000), now)).toBe(1_000);
    expect(relativeRefreshMs(new Date(now.getTime() - 10 * 60_000), now)).toBe(30_000);
    expect(relativeRefreshMs(new Date(now.getTime() - 5 * 3_600_000), now)).toBe(300_000);
    expect(relativeRefreshMs(new Date(now.getTime() - 3 * 86_400_000), now)).toBe(3_600_000);
  });
});

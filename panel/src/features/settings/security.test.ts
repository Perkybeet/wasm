import { describe, expect, it } from "vitest";

import { backupCodesFile, groupSecret, perWindow, readLockoutPolicy, spokenDuration } from "./security";

describe("security facts", () => {
  it("reads the lockout policy from the web block", () => {
    const policy = readLockoutPolicy({
      web: {
        max_failed_attempts: 5,
        lockout_duration: 900,
        rate_limit_enabled: true,
        rate_limit_requests: 120,
        rate_limit_window: 60,
        token_expiration_hours: 12,
        ip_whitelist: ["10.0.0.0/8"],
      },
    });
    expect(policy).toEqual({
      maxFailedAttempts: 5,
      lockoutSeconds: 900,
      rateLimitEnabled: true,
      rateLimitRequests: 120,
      rateLimitWindowSeconds: 60,
      sessionHours: 12,
      ipAllowlist: ["10.0.0.0/8"],
    });
    expect(readLockoutPolicy({}).maxFailedAttempts).toBeNull();
    expect(readLockoutPolicy({ web: { rate_limit_enabled: false } }).rateLimitEnabled).toBe(false);
  });

  it("says durations the way people do", () => {
    expect(spokenDuration(900)).toBe("15 minutes");
    expect(spokenDuration(60)).toBe("1 minute");
    expect(spokenDuration(3600)).toBe("1 hour");
    expect(spokenDuration(43_200)).toBe("12 hours");
    expect(spokenDuration(86_400)).toBe("1 day");
    expect(spokenDuration(45)).toBe("45 seconds");
    expect(spokenDuration(90)).toBe("1m 30s");
    expect(perWindow(60)).toBe("a minute");
    expect(perWindow(3600)).toBe("an hour");
    expect(perWindow(300)).toBe("every 5 minutes");
  });

  it("groups a base32 key in fours", () => {
    expect(groupSecret("JBSWY3DPEHPK3PXP")).toBe("JBSW Y3DP EHPK 3PXP");
    expect(groupSecret("JBSW Y3DP EH")).toBe("JBSW Y3DP EH");
  });

  it("writes the backup codes one per line", () => {
    expect(backupCodesFile(["a1b2-c3d4", "e5f6-a7b8"], "web-01")).toBe(
      "WASM backup codes for web-01\nEach code signs in once in place of an authenticator code.\n\na1b2-c3d4\ne5f6-a7b8\n",
    );
  });
});

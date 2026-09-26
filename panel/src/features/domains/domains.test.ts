import { describe, expect, it } from "vitest";

import { ApiError } from "../../api/errors";
import type { Job } from "../../api/queries/jobs";
import { createSiteBody } from "./CreateSiteDialog";
import { issueRequest } from "./IssueCertificateDialog";
import { byUrgency, certificateJobFor, certificateView, covers, issuerName } from "./certificates";
import { configRejection, failingLine, lineOffset } from "./configErrors";
import { dnsVerdict, isPrivateAddress, recordsToCreate } from "./dns";
import { domainProblem, parseNames, truncatedNames, wwwOf } from "./names";

describe("domain names", () => {
  it("accepts ordinary and internationalised names, trimmed and lowercased", () => {
    expect(domainProblem("shop.example.com")).toBeNull();
    expect(domainProblem("  Shop.Example.COM ")).toBeNull();
    expect(domainProblem("xn--bcher-kva.example")).toBeNull();
    expect(domainProblem("a-b.c-d.es")).toBeNull();
  });

  it("refuses what the server would refuse, in the operator's terms", () => {
    expect(domainProblem("")).toMatch(/Enter a domain/);
    expect(domainProblem("https://example.com")).toMatch(/without http/);
    expect(domainProblem("example.com/path")).toMatch(/no path/);
    expect(domainProblem("example.com:8080")).toMatch(/no path, port/);
    expect(domainProblem("localhost")).toMatch(/two parts/);
    expect(domainProblem("-bad.example.com")).toMatch(/hyphen/);
    expect(domainProblem("example..com")).toMatch(/empty parts/);
    expect(domainProblem("example.c0m")).toMatch(/letters only/);
  });

  it("reads a list of names from free text, each once, in order", () => {
    expect(parseNames("a.example.com, B.example.com\nc.example.com  a.example.com;")).toEqual([
      "a.example.com",
      "b.example.com",
      "c.example.com",
    ]);
    expect(parseNames(" \n ")).toEqual([]);
  });

  it("names the www twin, except of a www name", () => {
    expect(wwwOf("Example.com")).toBe("www.example.com");
    expect(wwwOf("www.example.com")).toBeNull();
  });

  it("shows the first few names of a list and counts the rest", () => {
    expect(truncatedNames(["a.com", "b.com", "c.com", "d.com"])).toEqual({ shown: "a.com, b.com, c.com", rest: 1 });
    expect(truncatedNames(["a.com", "b.com"])).toEqual({ shown: "a.com, b.com", rest: 0 });
    expect(truncatedNames(["a.com", "b.com", "c.com"], 2)).toEqual({ shown: "a.com, b.com", rest: 1 });
    expect(truncatedNames([])).toEqual({ shown: "", rest: 0 });
  });
});

describe("certificate state", () => {
  it("is valid, expiring under 21 days, expiring today, expired, or unknown", () => {
    expect(certificateView({ days_remaining: 46 })).toEqual({ tone: "ok", label: "Valid for 46 days", attention: false });
    expect(certificateView({ days_remaining: 21 })).toMatchObject({ tone: "ok" });
    expect(certificateView({ days_remaining: 20 })).toEqual({ tone: "warn", label: "Expires in 20 days", attention: true });
    expect(certificateView({ days_remaining: 1 })).toMatchObject({ label: "Expires in 1 day" });
    expect(certificateView({ days_remaining: 0 })).toMatchObject({ tone: "warn", label: "Expires today" });
    expect(certificateView({ days_remaining: -1 })).toMatchObject({ tone: "fail", label: "Expired yesterday" });
    expect(certificateView({ days_remaining: -9 })).toMatchObject({ tone: "fail", label: "Expired 9 days ago" });
    expect(certificateView({ days_remaining: null })).toMatchObject({ tone: "idle", label: "Expiry unknown" });
  });

  it("sorts the most urgent first and the unknown last", () => {
    const certs = [{ days_remaining: 40 }, { days_remaining: null }, { days_remaining: -2 }, { days_remaining: 5 }];
    expect([...certs].sort(byUrgency).map((cert) => cert.days_remaining)).toEqual([-2, 5, 40, null]);
  });

  it("finds the certificate job working on a lineage, or renewing them all", () => {
    const job = (type: string, status: string, domain: string) => ({ id: domain, type, status, metadata: { domain } }) as unknown as Job;
    const jobs = [job("cert_renew", "completed", "a.com"), job("deploy", "running", "b.com"), job("cert_create", "running", "b.com")];
    expect(certificateJobFor(jobs, "a.com")).toBeNull();
    expect(certificateJobFor(jobs, "b.com")?.type).toBe("cert_create");
    expect(certificateJobFor([job("cert_renew", "pending", "all")], "c.com")?.id).toBe("all");
  });

  it("covers a name it is named after or lists", () => {
    const cert = { domain: "a.com", domains: ["a.com", "www.a.com"] };
    expect(covers(cert, "www.a.com")).toBe(true);
    expect(covers(cert, "blog.a.com")).toBe(false);
    expect(covers(null, "a.com")).toBe(false);
  });

  it("reads the organisation and common name out of the issuer's distinguished name", () => {
    expect(issuerName("C = US, O = Let's Encrypt, CN = R11")).toBe("Let's Encrypt R11");
    expect(issuerName("CN=example-ca")).toBe("example-ca");
    expect(issuerName("not a distinguished name")).toBe("not a distinguished name");
  });
});

describe("DNS checks", () => {
  const check = (resolved: string[], here: boolean) => ({ points_here: here, resolved_addresses: resolved });

  it("says here, elsewhere or missing", () => {
    expect(dnsVerdict(check(["203.0.113.10"], true))).toBe("here");
    expect(dnsVerdict(check(["198.51.100.23"], false))).toBe("elsewhere");
    expect(dnsVerdict(check([], false))).toBe("missing");
  });

  it("proposes A and AAAA records for public addresses only", () => {
    expect(recordsToCreate(["203.0.113.10", "10.0.0.5", "2001:db8::10", "fd00::1"])).toEqual([
      { type: "A", value: "203.0.113.10" },
      { type: "AAAA", value: "2001:db8::10" },
    ]);
  });

  it("knows the private ranges", () => {
    for (const address of ["10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1", "100.64.0.1", "fd12::1"]) {
      expect(isPrivateAddress(address)).toBe(true);
    }
    for (const address of ["172.32.0.1", "203.0.113.10", "2001:db8::10"]) expect(isPrivateAddress(address)).toBe(false);
  });
});

describe("configuration test failures", () => {
  it("finds the line nginx and Apache point at", () => {
    expect(
      failingLine(
        'nginx: [emerg] unexpected "}" in /tmp/wasm-validate-1a2b/example.com:57\nnginx: configuration file /tmp/wasm-validate-1a2b/wasm-validate.conf test failed\n',
      ),
    ).toBe(57);
    expect(failingLine("AH00526: Syntax error on line 12 of /etc/apache2/sites-enabled/example.com.conf:\nInvalid command 'Foo'")).toBe(12);
    expect(failingLine("nginx: [warn] conflicting server name\nsomething else")).toBeNull();
  });

  it("reads a refusal from the API error: the backend's sentence and the server's words", () => {
    const refused = new ApiError(
      400,
      "validationerror",
      "nginx rejected the configuration for example.com",
      null,
      null,
      null,
      'nginx: [emerg] unknown directive "lisen" in /tmp/x/example.com:3\n',
    );
    expect(configRejection(refused)).toEqual({
      summary: "nginx rejected the configuration for example.com",
      output: 'nginx: [emerg] unknown directive "lisen" in /tmp/x/example.com:3\n',
      line: 3,
    });
    expect(configRejection(new ApiError(403, "elevation_required", "Confirm it's you"))).toBeNull();
    expect(configRejection(new Error("boom"))).toBeNull();
  });

  it("puts the caret at the start of a line", () => {
    const text = "one\ntwo\nthree";
    expect(lineOffset(text, 1)).toBe(0);
    expect(lineOffset(text, 3)).toBe(8);
    expect(lineOffset(text, 9)).toBe(text.length);
  });
});

describe("issuing a certificate", () => {
  const form = { domain: " Example.com ", names: "", includeWww: true, method: "auto" as const, webroot: "", email: "", expand: false };

  it("sends the names, the method and the options as the API takes them", () => {
    expect(issueRequest({ ...form, names: "shop.example.com, example.com", method: "webroot", webroot: " /var/www/html ", email: "ops@example.com", expand: true })).toEqual({
      request: {
        domain: "example.com",
        email: "ops@example.com",
        domains: ["shop.example.com"],
        method: "webroot",
        webroot: "/var/www/html",
        include_www: true,
        expand: true,
      },
    });
  });

  it("leaves the method to WASM when automatic, and www out when it is listed already", () => {
    expect(issueRequest({ ...form, names: "www.example.com" })).toMatchObject({
      request: { method: null, webroot: null, email: null, domains: ["www.example.com"], include_www: false },
    });
  });

  it("says what is wrong, field by field", () => {
    expect(issueRequest({ ...form, domain: "", names: "ok.example.com bad..name", method: "webroot", email: "nope" })).toEqual({
      errors: {
        domain: expect.stringMatching(/Enter a domain/) as string,
        names: expect.stringMatching(/^bad\.\.name: /) as string,
        webroot: expect.stringMatching(/directory the site serves/) as string,
        email: expect.stringMatching(/ops@example.com/) as string,
      },
    });
    expect(issueRequest({ ...form, method: "webroot", webroot: "var/www" })).toMatchObject({ errors: { webroot: expect.stringMatching(/absolute/) as string } });
  });
});

describe("creating a site", () => {
  const form = { domain: "status.example.com", webserver: "nginx" as const, template: "proxy" as const, port: "4000", ssl: true, enable: true };

  it("sends the proxy's port, and the default for static files", () => {
    expect(createSiteBody(form)).toEqual({ body: { domain: "status.example.com", webserver: "nginx", template: "proxy", port: 4000, ssl: true, enable: true } });
    expect(createSiteBody({ ...form, template: "static", port: "" })).toMatchObject({ body: { template: "static", port: 3000 } });
  });

  it("asks for a port for any template but static, not a fixed list of names", () => {
    expect(createSiteBody({ ...form, template: "advanced", port: "" })).toEqual({
      errors: { port: expect.stringMatching(/between 1 and 65535/) as string },
    });
    expect(createSiteBody({ ...form, template: "monorepo" })).toMatchObject({ body: { template: "monorepo", port: 4000 } });
  });

  it("refuses a bad domain and a bad port", () => {
    expect(createSiteBody({ ...form, domain: "nope", port: "70000" })).toEqual({
      errors: { domain: expect.stringMatching(/two parts/) as string, port: expect.stringMatching(/between 1 and 65535/) as string },
    });
  });
});

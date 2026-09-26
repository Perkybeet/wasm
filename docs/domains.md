# Domains

An application in WASM 2.0 answers on one primary domain and any number of aliases and
redirects. Every name is a row in the store, the web server site is rendered from those rows,
and one certificate covers all of them.

## Kinds

| Kind | What the web server does |
|---|---|
| `primary` | The application's own domain: the one it was created with, the one every command names it by. Exactly one per application, and it cannot be removed. |
| `alias` | Serves the application exactly like the primary (listed in the same `server_name`). |
| `redirect` | Answers with a permanent redirect (`301`) to the primary, keeping the path and query: `https://` when the site has a certificate, `http://` otherwise. |

A name belongs to at most one application. Adding a name that is already another
application's alias, redirect or primary is refused.

## Commands

```bash
wasm domain list shop.example.com                        # primary, then aliases, then redirects
wasm domain add shop.example.com shop.example.org        # alias (the default kind)
wasm domain add shop.example.com old-shop.example.com --kind redirect
wasm domain remove shop.example.com shop.example.org
```

`wasm domain list` takes `--json`. `wasm domain add` takes `--no-cert` to skip extending the
certificate now; run the same command again later to extend it.

Each change goes through the same steps, in this order:

1. **The store** records the name, refusing one that is not a domain, belongs to another
   application, or would be a second primary.
2. **The site** is rendered again by the application's own deployer, exactly as a deploy
   renders it, and the whole web server configuration is tested before the reload. A change
   the web server refuses is put back, the row and the file both, and the error shows the web
   server's own output.
3. **The certificate** is expanded under the same certbot lineage to cover every name,
   redirects included (a browser reaching `https://old-shop.example.com` needs a valid
   certificate before it can be redirected). This only happens for an application that
   already serves TLS; see below.

The unit, the build and the release are not touched: adding a domain does not restart the
application.

### `www`

`wasm create --www` records `www.<domain>` as a **redirect** to the domain. To serve the
`www` name as the canonical one instead, create the application on `www.example.com` and add
`example.com` as a redirect:

```bash
wasm create -d www.example.com -s git@github.com:you/site.git
wasm domain add www.example.com example.com --kind redirect
```

`wasm site create --www` (a bare site, not an application) is unchanged: it serves both names
and covers both with the certificate.

### Applications deployed before 2.0

The store migration gives every existing application its primary domain and nothing else,
because whether a 1.x deploy also served `www` was never recorded. The live site is not
changed by the upgrade: a 1.x application deployed with `--www` keeps serving both names.

The first `wasm domain add` or `wasm domain remove` on such an application reads the names its
live site answers on and records the ones the store does not have as aliases (the result
reports them as `adopted`), so adding one name never silently drops another. To turn an
adopted `www` alias into a redirect, remove it and add it back with `--kind redirect`.

## Certificates

- When an application serves TLS, `wasm domain add` extends its certificate to the new name
  right away. If certbot fails (typically because DNS does not point here yet), the domain is
  kept, the site keeps serving TLS with the certificate it had, and certbot's own output is
  shown. Run the same `wasm domain add` again once DNS is right: adding a name the application
  already has retries its certificate.
- When an application does not serve TLS, a new name is served over plain HTTP like the rest
  of it. Obtain the first certificate with `wasm cert create -d <primary> -d <alias> ...`.
- Removing a name does not revoke anything. The certificate keeps covering the removed name
  until it is next issued with a different set of names.
- Through the API, `POST /api/apps/{domain}/domains` returns as soon as the name is recorded
  and the site reloaded; extending the certificate is queued as a job whose id is returned as
  `certificate_job_id`. Follow it with `GET /api/jobs/{id}` or the `/events` stream.

Certificates come from Let's Encrypt through certbot, which rate limits issuance. A
certificate that already covers the requested names is left alone.

## DNS check

Before asking for a certificate, check that the name resolves to this server:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://panel.example.com/api/apps/shop.example.com/domains/shop.example.org/dns
# {"domain":"shop.example.org","expected_addresses":["203.0.113.10"],
#  "resolved_addresses":["203.0.113.10"],"points_here":true}
```

`GET /api/domains/dns?name=<domain>` runs the same check for a name that is not attached to
any application yet; the new-app wizard in the console uses it. The console also runs the
check when you add a name to an application, and from each domain's menu on the Domains tab,
showing this server's addresses next to the ones the name resolves to.

How it decides:

- The name is resolved with the system resolver (`getaddrinfo`, A and AAAA).
- It is compared with the addresses of this machine's network interfaces, leaving out
  loopback, link-local, multicast and unspecified addresses.
- `points_here` is true when the name resolves and every address it resolves to is one of
  this machine's.

Known false negatives:

- **Behind NAT** (cloud instances whose public address is mapped by the provider, as on AWS
  and Google Cloud, and servers at home or in an office), the machine only sees its private
  address, so a name pointing at the public address reads as not pointing here.
- **Behind a proxying CDN** such as Cloudflare with the proxy enabled, the name resolves to
  the CDN's addresses, and the check reports it as not pointing here.

In both cases the check is advisory. `wasm domain add` runs it first and prints a warning with
both address lists when the name does not point here, then goes ahead; `wasm cert create`
does not run it. Certbot is the final judge.

## Limitations

- Monorepo and Docker Compose applications write the web server configuration of their
  services themselves, and do not support aliases or redirects yet. `wasm domain add`
  refuses them; serve another name with its own application or site instead.
- `wasm domain` changes need an application deployed by WASM. For a bare site, use
  `wasm site` and `wasm cert`.
- Wildcard names (`*.example.com`) are not accepted as domains.

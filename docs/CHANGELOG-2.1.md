# WASM 2.1 changelog

Changes since 2.0.1. Upgrade notes are in [UPGRADING-2.0.md](UPGRADING-2.0.md#21).

## Licence

WASM is free software from 2.1.0: **GNU Affero General Public License, version 3 or later**.
Anyone may use it, commercially included, study it and change it; a changed version offered
to others over a network must offer its source. Releases up to 2.0.x stay under WASM-NCSAL 1.0.

## Deploys

- **Health check per application**: path, accepted statuses and timeout (`wasm app health`,
  Settings → Releases, `PATCH /api/apps/{domain}/health`), used by every gate and by diagnose.
  The timeout is a real deadline.
- **Release retention** is configurable: `wasm releases keep DOMAIN N`, the console, or
  `PATCH /api/apps/{domain}/releases/retention`; pruning happens at once and never removes the
  active release or the rollback target.
- **Rebuild a deployment's exact commit**: `wasm update DOMAIN --commit SHA` and "Rebuild this
  commit" on a deployment's page. On releases, a release of that commit still on disk
  activates in seconds.
- **Roll back to a deployment** from its page, in place too: on a git checkout it redeploys
  that commit behind the health gate, leaving uploads, SQLite files and `.env` alone.
- **"Nothing new to deploy"**: an update from the console or the CLI asks before rebuilding the
  commit that is already live (compared with the last successful deployment); webhooks never
  ask.
- **Docker Compose and monorepo updates pass the health gate** and put back what served when
  the new version does not answer; volumes are never touched.
- The application's unit no longer outlives a change to a static type, and a unit that can
  never start ends in `failed` instead of restarting forever (2.0.1, now also on deploy).

## Console

- `wasm web enable` runs the console as a systemd service that survives reboots and restarts
  on upgrades; `wasm web disable` removes it. The first start explains the SSH tunnel and how
  to keep it running.
- Charts show the time and values under the pointer (and from the keyboard) and open large,
  with zoom. Pages use up to 1600 px, and layout shift is measured on every page by the test
  suite.
- Settings: the email account (SMTP) and its recipients are editable; Telegram finds the chat
  id for you and shows Telegram's own reason when a test fails.
- Server health lists the reasons behind its verdict; an expired certificate is called
  expired and links to it. Backups show the disk that holds them and where misplaced backups
  were found, with the command that moves them.
- The deployment page: rebuild this commit, roll back to this deployment (or why not), phase
  timings on the log's own clock (a time-zone offset showed as "1h 0m"), "not applicable"
  health on static sites.
- The new-application wizard reads only the files it needs from a repository, says whether
  WASM can deploy it and what to do if not, and Cancel stops the work on the server.
- Docker Compose applications say their containers are measured by Docker instead of
  charting zeros.

## Diagnosis

- A failed certificate order names a DNS record that points elsewhere (an AAAA record on
  another server, say) above certbot's own words.
- Apache counts only when it is installed and in use.
- Per-application CPU and memory are sampled again: since 0.14.1 the collector looked for
  unit names no application had.

## Security

- Stored source URLs with credentials in them no longer put the credential on a command line
  or in a log; git gets it from its environment, for that host only.
- The rate limiter looks at credentials only where they are used, and a wrong one counts
  toward the lockout.
- The SMTP password is kept on save only while the server, user and TLS settings stay the
  same.
- WASM's own units (console, monitor, cron and backup jobs) cannot be driven through the
  services API, and their journals need admin.
- Repository inspection never follows symlinks and never returns a secret-looking default.
- `wasm config set --stdin` and `--prompt` keep secrets off the command line.

## Fixes

- Editing a cron job no longer doubles the escaping of `$` and `%` in its command, and saving
  it from the console keeps its application.
- `$` in a cron command reaches the shell as typed.
- Two backups taken in the same second no longer share an id.
- `wasm web enable` retires the previous access token before restarting.
- Asking for `wasm-web` never acts on an application named `web`.
- The update notice goes to standard error and never into `--json` output.
- `--dry-run` works after the new subcommands; man pages carry the right licence and version.
- A failed job keeps the tool's own output (certbot's, nginx's) under its message.

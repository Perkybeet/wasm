# Releases

WASM 2.0 builds every deploy of a new application in its own directory, switches to it
atomically, keeps it only if it answers, and can go back to any release still on disk in
seconds. This page describes that layout, how a deploy moves through it, and how an
application deployed by 1.x is moved onto it.

## Two layouts

| Layout | Who gets it | Deploy | Rollback |
|---|---|---|---|
| `releases` | Every application created by 2.0, unless told otherwise | A new directory per deploy, health-gated activation, automatic rollback | Re-point `current` and restart: seconds |
| `inplace` | Every application deployed by 1.x, and types that cannot build releases yet | Backup, pull and rebuild over the tree the service is running, restart. A failed health check after the restart is reported, not rolled back. | Restore a backup |

The layout of a new application comes from `--layout` on `wasm create` (or `layout` on
`POST /api/apps`), and otherwise from the `deploy.layout` setting, which defaults to
`releases`:

```bash
wasm config get deploy.layout
wasm config set deploy.layout inplace    # new applications build in place, as in 1.x
```

An existing application always keeps the layout it has. A deploy or an update never changes
it; asking for a different layout on a redeploy is refused with an error. The only way from
`inplace` to `releases` is the explicit migration described [below](#moving-an-in-place-application-onto-releases).

Monorepo and Docker Compose applications cannot build releases yet. With the server default
they are deployed in place, as before; asking for `--layout releases` explicitly is an error
rather than a silent downgrade.

## Layout on disk

```
/var/www/apps/{app}/
  releases/20260925-143012-a1b2c3d/   one complete build; never rebuilt once it is active
  releases/20260924-101500-9f8e7d6/
  current -> releases/20260925-143012-a1b2c3d
  shared/.env                          the environment, mode 0600, outside every release
  shared/<persistent paths>            uploads, storage, data: linked into each release
  repo/                                git cache the releases are exported from
```

`{app}` is the domain with dots replaced by dashes (`shop.example.com` becomes
`shop-example-com`). The systemd unit and the web server site point at `current`, never at a
release directly, so the same unit and site serve whichever release is active.

- A release id is `YYYYMMDD-HHMMSS-<commit>`: the UTC creation time and the first seven
  characters of the commit, or `nogit` for a source that is not a git repository. A second
  release created in the same second gets `-2`, and so on.
- Every link is relative, so the application directory can be moved or restored from a
  backup elsewhere and still resolve.
- `repo/` is an ordinary clone that is fetched and reset on each deploy; the target commit is
  exported into the new release without `.git`. Local directories and archives are copied or
  extracted straight into the release.

## What a deploy does

For an application on `releases`, a deploy (`wasm create`, the new-application wizard) and an
update (`wasm update`, the console's Update button, `POST /api/jobs/update`, a push webhook)
run the same sequence:

1. **Fetch into a new release.** The source is exported into `releases/<id>/`. The running
   application does not see this directory.
2. **Link what releases share.** `shared/.env` is linked into the release when it exists,
   and each persistent path becomes a link to `shared/<path>`. This happens before the
   install and the build, because both may need the environment or write into a persistent
   directory.
3. **Install dependencies, or reuse them.** When every lockfile (`package-lock.json`,
   `pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`, `requirements.txt`, `poetry.lock`, `uv.lock`,
   `Pipfile.lock`) is byte-identical to the active release's, `node_modules`, `.venv` or
   `venv` is copied from the active release with `cp -a --reflink=auto` instead of being
   reinstalled. The deploy log says `Dependencies reused from <release>`. Otherwise the
   install runs normally.
4. **Build** inside the release.
5. **Hand the tree over** to the service user.
6. **Write the site, the certificate and the unit**, all pointing at `current`. Only a
   deploy (`wasm create`, `POST /api/apps`) does this; an update skips it, because the unit
   and the site already point at `current`.
7. **Activate behind the health gate.** See the next two sections.
8. **Prune** releases beyond the retention.

`wasm update` on an application on `releases` takes no backup first: the release that was
serving stays on disk and is what an automatic or manual rollback returns to.

## The health gate

Activation swaps `current` atomically: a new link is created next to it as
`current.tmp-<random>` and renamed over the old one, so `current` resolves at every instant.
Then the unit is restarted and probed.

| Application | Probe |
|---|---|
| Runs a process (Node, Next.js, Python, ...) | `GET http://127.0.0.1:<port>/`, up to 15 attempts 2 seconds apart, 5 seconds each. Any status below 500 passes, redirects included. |
| Serves files only (static sites, Vite builds without SSR) | The files it serves are there: an `index.html` in the directory the web server serves. |

The same gate judges a deploy, an update, an instant rollback, a migration and a change of
resource limits with `--restart`, so none of them can activate something another would have
refused.

## Automatic rollback

When a new release fails the gate:

1. The release is recorded as `failed` and its directory is removed.
2. `current` is pointed back at the release that was serving, and the unit restarted and
   probed again.
3. The deployment fails with the evidence verbatim: every failed probe (identical
   consecutive failures folded into one line) and the last 40 lines of the unit's journal.

The error says which release is active again and whether it answered. A failed release stays
in `wasm releases list` for a while, marked as not on disk, and cannot be activated. If the
very first release of an application fails, there is nothing to go back to and the deploy
fails; a first deploy that fails undoes the steps it ran.

## Instant rollback

```bash
wasm releases list shop.example.com                  # newest first; the active one has an asterisk
wasm releases rollback shop.example.com              # the release created just before the active one
wasm releases rollback shop.example.com 20260924-101500-9f8e7d6
```

Nothing is rebuilt: `current` is re-pointed and the unit restarted, behind the same health
gate. If the target does not answer, the release that was serving is put back and the command
fails with the probe's and the journal's output. Rolling forward works the same way: name a
newer release.

In the console, the Deployments tab of an application lists its releases; activating one is
the same operation. Over the API:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://panel.example.com/api/apps/shop.example.com/releases/20260924-101500-9f8e7d6/activate
```

This needs a token with the `deploy` scope, the same as queueing an update. See
[api.md](api.md).

Release statuses, as `wasm releases list --json` and `GET /api/apps/{domain}/releases`
report them: `active`, `superseded`, `rolled_back`, `failed`, `built`. `active` is whatever
`current` points at, regardless of what the store row says.

### Releases and backups

`wasm releases rollback` and `wasm rollback` are different tools:

- `wasm releases rollback` switches between builds that are already on disk. It is instant,
  and it does not touch `shared/`: the environment and uploaded files are the same before and
  after.
- `wasm rollback` restores a backup archive, taking a safety backup of the current state
  first. It is the recovery tool for data and for in-place applications. See `wasm backup`.

## Persistent paths and `shared/`

Anything an application writes for itself that must survive a deploy (user uploads, a
SQLite file, `storage/`) has to live in `shared/`, because every deploy starts from a fresh
release directory.

- `.env` is always shared. `wasm env`, the console's Environment tab and the backups read
  and write `shared/.env` for an application on `releases`, and `.env` in the application
  directory for one in place.
- Other paths are declared per application: `wasm create --persist storage --persist
  public/uploads`, or `persistent_paths` on `POST /api/apps`. Paths are relative to the
  application root and may not be absolute or contain `..`.
- If a release already contains something at a persistent path (usually a file tracked in the
  repository), it is left exactly as it is and reported as a conflict in the deploy log. The
  tracked copy wins; untrack it or drop it from the persistent paths.
- WASM never writes through a symlink found inside a release or under `shared/`. A
  repository is untrusted input, and a tracked link to `/etc` must not become a place WASM
  writes to as root.

## Retention

After a successful activation, releases beyond the newest five are deleted, oldest first.
The active release is never deleted, even when a rollback made it older than the five
newest, and neither is the release just before it, which is what `wasm rollback` goes back to.
Rows of failed releases stay listed while they are among the newest five and are
forgotten after that.

The retention is recorded per application (`keep_releases` in `GET /api/apps/{domain}`,
default 5). There is no command or endpoint to change it in 2.0.

## Moving an in-place application onto releases

An application deployed by 1.x keeps running and updating in place after the upgrade, exactly
as before. Moving it onto releases is an explicit operation, one application at a time:

```bash
wasm --dry-run app migrate shop.example.com     # the plan, and a rehearsal that changes nothing
wasm app migrate shop.example.com               # asks for confirmation; -y to skip it
wasm app migrate shop.example.com --persist storage --persist public/uploads
```

`--dry-run` is a global option and goes before the command. The console offers the same
operation from the application's Settings tab; the API has `GET /api/apps/{domain}/migrate/plan`
(read only) and `POST /api/apps/{domain}/migrate` (needs sudo mode), which queues the migration
as a job and answers `202` with it.

What the migration does:

1. **The unit is stopped first**, so nothing writes into the tree while it moves. The plan
   states this downtime; it lasts until the health gate passes, usually seconds.
2. **The live tree becomes the first release.** It is moved, not copied: every change is a
   rename inside the application directory, or the creation of a directory or a link.
3. **The environment moves to `shared/`.** `.env` and WASM's inventory of it (`.wasm`) are
   moved to `shared/` and linked into the release.
4. **What the application wrote for itself moves to `shared/`.** In a git checkout that is
   every untracked directory, ignored or not (`git status --ignored`), minus build output
   (`node_modules`, `.next`, `dist`, `build`, `.venv`, `__pycache__` and similar). Without
   git, WASM cannot tell uploads from code: it keeps whichever of `uploads`,
   `public/uploads`, `storage` and `data` exist, and warns you to name the rest. `--persist`
   replaces detection entirely. SQLite databases are found by their file header wherever
   they are (outside build output) and always move to `shared/`, with their `-wal`, `-shm`
   and `-journal` files beside them; a `--persist` list that leaves one out is refused.
5. **The unit and the site are rewritten to run from `current`**, when they name the
   application directory. A proxied site only names a port and is left alone.
6. **The file count is verified.** Regular files and their bytes are counted before and after,
   `shared/` included. Any difference fails the migration.
7. **The application goes through the health gate.** If it does not answer, every rename is
   reversed, the unit and site are put back byte for byte, and the application is restarted
   on the tree it had. Every undo step is attempted even when an earlier one fails; anything
   that could not be put back is listed in the error with where it is now.

Read the plan before confirming. Two warnings in it matter:

- **Untracked files outside a persistent path** stay in the first release only. The next
  deploy will not have them. Name the directory that holds them with `--persist`.
- **A tree that is not a git checkout** gets only the usual upload directories. Anything else
  the application writes must be named with `--persist`.

The migration refuses an application that is already on releases, one whose type cannot build
releases (monorepo, Docker Compose), and one whose directory is missing. After it, the first
`wasm update` builds a second release from the recorded source through `repo/`.

## Deploy history

Every deploy, update, activation and migration writes a row to the deployment history with
its build log, whatever the layout, monorepo and Docker Compose included. See it with the
console's Deployments tab or `GET /api/deployments?domain=<domain>`; the last 20 per
application are kept.

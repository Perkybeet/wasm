# Resource monitor

`wasm monitor` watches the machine and writes down what stands out: resource use, processes
worth a look, units that stopped, certificates about to expire and full disks. It keeps what
it noticed in a local database and can send you a report by email, or through any notification
channel you configure.

It reports and does nothing else:

- it never signals, terminates or restarts a process;
- it never deletes or modifies a file, except the systemd unit it installs;
- it never decides anything from a process's command line;
- what it sends leaves the machine only through channels you configured: raw observations by
  email when `monitor.notify` is on, and the disk, certificate and unit events through
  whichever notification channels you set up - webhook, Slack, Discord, Telegram or email (see
  [Notifications](#notifications)). Nothing is sent anywhere else.

Before 1.0 the monitor sent process data to OpenAI and could kill processes and delete their
files. All of that is gone. The flags `--force-ai` and `--all` on `wasm monitor scan` are
still accepted, ignored with a warning; the settings `monitor.auto_terminate`,
`monitor.terminate_malicious_only` and `monitor.dry_run` are pinned to their safe values
whatever the file says; `monitor.use_ai`, `monitor.ai_interval` and `monitor.openai.*` have no
effect.

## Commands

```bash
wasm monitor scan          # look at the machine once and print what stands out
wasm monitor run           # scan on a loop in this terminal, until Ctrl+C
wasm monitor install       # write the systemd unit, without starting it
wasm monitor enable        # start it now and at every boot, installing it if needed
wasm monitor disable       # stop it and keep it from starting at boot
wasm monitor uninstall     # remove the unit; recorded observations stay
wasm monitor status        # whether it runs, and what it watches
wasm monitor config        # the settings in effect and where the database lives
wasm monitor test-email    # send one email to the recipients, to prove the settings
```

The unit is `wasm-monitor.service`. It runs `wasm monitor run` as root, restarts on failure,
and is sandboxed (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateDevices`). The console's Server page has the same controls.

## What it watches

Every scan interval (60 seconds by default, never less than 10):

| Watch | Recorded when | Severity |
|---|---|---|
| Process names | The executable's name starts with a known miner or malware name: `xmrig`, `minerd`, `cpuminer`, `cgminer`, `bfgminer`, `ethminer`, `ccminer`, `kdevtmpfsi`, `kinsing`, `kerberods`, `watchdogs`. Common daemons are never flagged. | `warning` |
| Process resource use | A process uses more CPU or memory than `monitor.cpu_threshold` or `monitor.memory_threshold` percent (80 by default). | `notice` |
| Units | A unit WASM manages, or one listed in `monitor.watch_units`, fails: it is `failed`, it crash-loops (its automatic restarts grow between two scans), or it stopped after a failed run. A unit stopped on purpose does not count. All units are read with one `systemctl show` per scan; one message per outage. | notification `unit_failed` |
| Certificates | A certificate has less than 14 days left; at most one message per certificate per day. | notification `cert_expiring` |
| Disks | A filesystem crosses 90% used; one message per crossing. | notification `disk_threshold` |

Observations are kept in `/var/lib/wasm/observations.db` (`~/.local/share/wasm/` when run
without root). The same process and signal within an hour is recorded once; observations
older than `monitor.retention_days` (30) are purged hourly, and at most
`monitor.max_observations` (5000) are kept. Acknowledging one, from the console or
`POST /api/monitor/observations/{id}/acknowledge`, marks it seen and does nothing to the
process.

Certificate and disk checks, and the certificate expiry notifications, only happen while the
monitor runs.

## Email

```bash
wasm config set monitor.smtp.host smtp.example.com
wasm config set monitor.smtp.port 465
wasm config set monitor.smtp.username alerts@example.com
wasm config set monitor.smtp.password '...'
wasm config set monitor.email_recipients '["admin@example.com"]'
wasm config set monitor.notify true
wasm monitor test-email
```

With `monitor.notify` on, a scan that records new observations mails them to the
recipients. A process that keeps matching the same signal does not send a message every
scan. Credentials are only sent over TLS: `use_ssl` (implicit TLS, the default) or `use_tls`
(STARTTLS); without either, a configured password is refused.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `monitor.enabled` | `false` | Whether it is meant to run at boot |
| `monitor.scan_interval` | `60` | Seconds between scans, at least 10. Above 300, `wasm monitor status` warns that a failure may go unnoticed that long |
| `monitor.cpu_threshold` | `80.0` | CPU percent above which a process is recorded |
| `monitor.memory_threshold` | `80.0` | Memory percent above which a process is recorded |
| `monitor.watch_units` | `[]` | Units watched in addition to every unit WASM manages |
| `monitor.notify` | `false` | Mail new observations |
| `monitor.retention_days` | `30` | Days observations are kept |
| `monitor.max_observations` | `5000` | Observations kept at most |
| `monitor.email_recipients` | `[]` | Who receives the mail |
| `monitor.smtp.host`, `.port`, `.username`, `.password` | `""`, `465`, `""`, `""` | SMTP relay |
| `monitor.smtp.use_ssl`, `.use_tls` | `true`, `false` | Implicit TLS, or STARTTLS |
| `monitor.smtp.from_address` | the username | Sender |
| `monitor.smtp.timeout` | `30` | Seconds, at most 120 |

`wasm config get` and `wasm config set` take these keys; secret values print as `***`. Give
`monitor.watch_units` as a list: `wasm config set monitor.watch_units
nginx.service,postgresql.service --list`.

## Notifications

The `unit_failed`, `cert_expiring` and `disk_threshold` events go through WASM's notification
channels (webhook, Slack, Discord, Telegram, email), which are configured under
`notifications.*` in the console's Settings > Notifications or with `wasm config set`, and
only when `notifications.enabled` is on and the event is enabled. Test a channel with
`wasm notify test <channel>`.

## API

| Endpoint | |
|---|---|
| `GET /api/monitor/status` | Unit state, and the list of things the monitor never does |
| `GET /api/monitor/config` | Settings in effect |
| `GET /api/monitor/metrics` | One live reading of CPU, load, memory, swap, disks, network |
| `GET /api/monitor/processes` | The process table, sortable, at most 500 |
| `POST /api/monitor/scan` | Run one scan now |
| `GET /api/monitor/observations` | Recorded observations and their counts |
| `POST /api/monitor/observations/{id}/acknowledge` | Mark one as seen |
| `POST /api/monitor/install`, `/uninstall`, `/enable`, `/disable`, `/start`, `/stop` | Manage the unit |
| `POST /api/monitor/test-email` | Send a test email |

## Troubleshooting

- **Nothing is recorded.** `psutil` must be installed (`apt install python3-psutil`, or
  `pip install psutil`), and the monitor needs root to see every process.
- **No email.** Run `wasm monitor test-email`: it prints the SMTP server's own error. Check
  that the firewall allows outbound 465 or 587.
- **Too many resource-use observations.** Raise `monitor.cpu_threshold` or
  `monitor.memory_threshold`. The same process with the same signal is recorded at most once
  an hour.

# v2uptime

v2uptime checks V2Ray share links by connecting through Xray, on a timer (60 seconds by default), and publishes the result on a public status page and in a Telegram bot. Times in the interface are Gregorian, 24-hour, with Latin digits. The database stores UTC Unix epochs.

## Overview

One Xray process serves every server. Each server gets a local SOCKS inbound. A check opens a fresh connection through that inbound, completes the tunnel (including TLS or REALITY), and requests the configured test URL. A 2xx response is a success. Latency is the full request time, not an ICMP ping.

The public page lists each server’s status, ping, and uptime. The admin panel adds servers, subscriptions, incidents, Telegram, and settings. Share links, addresses, and ports are not shown on the public page, in the public API, or in Telegram messages.

## Features

- Protocols: `vless`, `vmess`, `trojan`, `ss`. Transports: `tcp` (optional HTTP header), `ws`, `grpc`, `httpupgrade`, `xhttp`, `kcp`. Security: `none`, `tls`, `reality`.
- Add one link, many links (one per line), or a subscription that syncs on a schedule.
- Add, edit, and remove servers through the Xray API while the process stays up. If a hot reload fails, Xray restarts once and the check cycle that overlapped the restart is discarded.
- Detects when the monitor’s own internet is down (a direct reference request) and keeps those results out of uptime.
- Incidents use an N/M rule. Flapping is detected. Incident stats include count, downtime, mean time to recover, and longest outage.
- Public page: one status banner, a server list (flag, status, ping, 24h uptime), Fastest and Most stable sorting, and a row that opens 24h/7d/30d uptime, a 24h status bar, the last incident, and a ping chart. Light and dark theme. Refreshes every 60 seconds.
- Telegram bot with a durable queue. Messages go out directly, or through an online server when `api.telegram.org` is not reachable from the monitor.
- Fonts, icons, and charts are served by the app. No third-party CDN.
- One-command install, a management menu, and support for a proxy, mirrors, and an offline archive.

## Screenshots

| Public page | Server detail | Mobile | Admin dashboard |
|---|---|---|---|
| ![Public status page](docs/screenshots/status-page.jpg) | ![Server detail](docs/screenshots/server-detail.jpg) | ![Mobile status page](docs/screenshots/mobile.jpg) | ![Admin dashboard](docs/screenshots/admin-dashboard.jpg) |

## Requirements

- Ubuntu 22.04+ or Debian 12+ (Python 3.10+).
- Root, for the installer.
- A domain that points at the server, if you want HTTPS.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/MoonlessWanderer/v2uptime/main/install.sh | sudo -E bash
```

The script clones `MoonlessWanderer/v2uptime`. For another fork, set `V2UPTIME_REPO` or change `REPO_SLUG` at the top of `install.sh`.

It checks the OS and Python, installs packages (nginx, certbot, and the rest), downloads the source, creates a virtualenv, downloads the Xray build for the CPU architecture and checks its SHA256, asks for the domain, email, admin username, and password, obtains a Let’s Encrypt certificate, configures nginx on 443 with an 80→443 redirect, and installs a systemd service that runs as a non-root user. The `v2uptime` command is the management menu.

After install, the script prints the admin path (a random path such as `/panel-x7k2q9ab`).

| Flag | What it does |
|---|---|
| `--proxy socks5h://IP:PORT` or `--proxy http://IP:PORT` | Proxy for git, downloads, pip, and apt. `http_proxy` and `https_proxy` are also honored with `sudo -E`. |
| `--pip-mirror URL` | PyPI index, for example `https://mirror-pypi.runflare.com/simple`. |
| `--apt-mirror URL` | Temporary apt mirror for this run, for example `https://mirror.arvancloud.ir`. |
| `--xray-zip FILE` | Install Xray from a local `Xray-linux-64.zip`. |
| `--from-archive FILE` | Offline install from a release archive (source, pip wheels, Xray, and the font). |

Example with a proxy:

```bash
curl -fsSL https://raw.githubusercontent.com/MoonlessWanderer/v2uptime/main/install.sh | sudo -E bash -s -- --proxy socks5h://203.0.113.10:1080
```

Offline install: on a machine that has internet, download `v2uptime-<version>-linux-amd64.tar.gz` (or `arm64`) from Releases, copy it to the server, and run:

```bash
sudo bash install.sh --from-archive ./v2uptime-1.1.0-linux-amd64.tar.gz
```

GitHub Actions (`.github/workflows/release.yml`) builds those archives for each release, with wheels for CPython 3.10–3.13. Apt packages (nginx, certbot, python3-venv) are not inside the archive. In an offline install they still come from an apt mirror (`--apt-mirror`).

If Let’s Encrypt fails (port 80 closed, DNS wrong, or the CA unreachable), the script explains the usual causes and offers retry, an existing certificate (`fullchain` / `privkey`), or HTTP only for now.

Running the installer again keeps `config.py`, the database, and the admin account.

## Update

From the menu, choose **2) Update**. The new version is downloaded into a staging directory and a new virtualenv is built there before anything is replaced. `config.py`, `data/`, `logs/`, and `bin/` (the Xray binary) are kept. If the new version does not become healthy, the previous files and virtualenv are restored.

When GitHub is not reachable directly, the download (and pip) can go through a healthy monitored server. The script reads that server’s SOCKS address from `GET http://127.0.0.1:<port>/internal/proxy`. That endpoint answers only on localhost; nginx does not expose it.

Update from an offline archive:

```bash
sudo v2uptime --from-archive ./v2uptime-1.1.0-linux-amd64.tar.gz
```

Then choose **2) Update**.

## Management menu

`sudo v2uptime`

```
 1) Install
 2) Update
 3) Update Xray core
 4) Start / Stop / Restart
 5) View logs
 6) Domain and SSL
 7) Change port
 8) Service status
 9) Backup and restore database
10) Reset admin password
11) Uninstall
 0) Exit
```

Uninstall asks you to type `DELETE` before it removes the installation.

## Telegram bot

Open **Telegram** in the admin panel.

- Bot token (masked in the form; it is not written to the log).
- Chat IDs, comma-separated. Groups and channels use `-100…`.
- Send mode: `auto`, `direct`, or `via_server`.
- Preferred server (`0` is automatic).
- Which events to send.
- Daily report time and weekly report day.
- **Send test message** sends immediately (it is not queued) and shows the route.

`auto` tries a direct send with a 5 second connect timeout, then up to 4 online servers over local SOCKS (`socks5h`, so DNS goes through the tunnel). Order: preferred server, then highest stability score, then lowest latency. The last route that worked is tried first next time. The server an outage alert is about is not used to send that alert.

Every alert is stored in `TelegramOutbox` first. Checks never wait on Telegram. If no route works, the row is retried with backoff from 30 seconds up to 10 minutes, and the message still shows the real event time. More than 10 waiting messages for a chat are merged into a summary. Each message stays within 4096 characters. A 429 response honors `retry_after`. Rows that are not still pending are deleted after 7 days.

A down alert looks like this:

```
🔴 Server down: Germany-1
Since: 2026-10-31 18:28 (UTC+03:30)
Error: Timeout
Uptime (24h): 99.2%
```

A recovery:

```
🟢 Server back online: Germany-1
Downtime: 12m (18:28 → 18:40)
```

If the message left the queue at least 2 minutes late, a line is added: `Delivered 14m late.`

The daily and weekly reports are one line per server (uptime, ping, incident count, downtime), then Fastest and Most stable. The first run does not send a report immediately; the next scheduled time does.

Commands (long polling, only for registered chat IDs): `/status`, `/report`, `/incidents`.

## Configuration

Settings live in the database and can be changed in the admin panel without a restart, including the admin path.

| Setting | Default |
|---|---|
| Site title | Server Status |
| Site description | Real-time connection quality of our servers |
| Test location | empty (the line is hidden) |
| Timezone | Asia/Tehran |
| Check interval | 60 seconds |
| N (failures before an incident) | 3 |
| M (successes to close an incident) | 2 |
| Degraded latency | 1500 ms |
| Degraded failure share | 20% of checks in the last 10 minutes |
| Flap window / threshold | 30 minutes / 4 transitions |
| Raw check retention | 48 hours (minimum 26) |
| Hourly retention | 90 days |

Status, used by the public page, incidents, and Telegram:

| Status | Rule |
|---|---|
| Down | An incident is open: N failures in a row. It closes after M successes in a row. The incident starts at the first failure of that run and ends at the first success of the recovery run. |
| Degraded | Not down, but the last check failed, or latency is above the threshold, or more than the configured percent of the last 10 minutes failed. |
| Online | The last check succeeded and latency is under the threshold. |
| Unknown | Fewer than 2 valid checks, or the last valid check is older than 3 intervals. |

- Uptime % = `ok / (ok + fail)`. `unknown` checks (monitor network outage, Xray unavailable) are left out.
- Fastest ranks online servers that are not still collecting, by the median latency of the last hour.
- Most stable ranks servers that are not still collecting, by the 24h stability score.
- Stability score (0–100): `normalized_jitter = min(jitter_24h / 500ms, 1)`, then `(uptime_24h / 100 * 0.7 + (1 - normalized_jitter) * 0.3) * 100`. Jitter is the standard deviation of latency.
- A server with fewer than 10 valid checks in 24 hours is labeled **New** on the public page and is left out of both rankings.
- Monitor network outage: if every server fails in the same cycle and the reference URL (default `https://www.aparat.com`, no proxy) also fails, that cycle is stored as `unknown` and a single outage alert is sent (and a restore alert later).
- Flapping: more than 4 transitions in 30 minutes sends one warning and pauses per-server down/up messages until the server stays unchanged for half the window. A final message then reports the current status.
- A manual **Test now** is shown in the panel and is not stored in the stats.
- Configs removed from a subscription are deactivated, not deleted, so their history remains.
- Storage: SQLite in WAL mode with `busy_timeout`. Each cycle is written in one transaction. Raw rows are kept for the retention window; hourly rows cover 7d and 30d, plus raw rows not yet rolled up. Hourly aggregation is idempotent and catches up after downtime.
- JSON logs write `ts` as UTC with milliseconds, ending in `Z` (`2026-10-31T14:58:05.123Z`), regardless of the system timezone. Displayed times use the timezone setting (`UTC+03:30` for `Asia/Tehran`).

The app listens on `127.0.0.1` only (nginx in front). It runs as one worker because the scheduler lives in the process. Session cookies are `HttpOnly`, `Secure` when the site is HTTPS, and `SameSite=Lax`. Forms and JSON requests need a CSRF token. Login is limited to 5 failures per 15 minutes for each IP and username. Passwords are bcrypt. A random `secret_key` is written at install. Responses send CSP and `X-Frame-Options`.

## Public API

Read-only. Responses do not include share links, addresses, or ports. Hidden and inactive servers are omitted; asking for one by id returns 404.

- `GET /api/status?range=24h|7d|30d` — summary, status, and metrics for every public server, status bars for the range, and the Fastest and Most stable order.
- `GET /api/servers/{id}/history?range=24h|7d|30d` — ping and uptime series, a range summary, and incidents.
- `GET /healthz` — process health.

Responses are cached for at most 60 seconds and are invalidated after each check cycle.

Timestamps are RFC 3339 UTC (`2026-10-31T14:58:00Z`). `*_label` fields are Gregorian, 24-hour, with Latin digits (`2026-10-31 18:28`). `range_label` is `24h`, `7d`, or `30d`. Status and history payloads include `timezone`: `{"name": "Asia/Tehran", "offset": "+03:30"}`. Each server in `/api/status` has `last_incident` (`started_at`, `ended_at` or `null`, `duration_seconds`), or `null` when there was no incident in the last 30 days. History incidents include `started_at` and `ended_at` (ISO, or `null` while the incident is open) as well as the labels. A missing server returns `404` with `{"detail": "Server not found"}`.

The `use_jalali` setting has been removed. API errors are English.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip install -r requirements-dev.txt
python3 main.py create-admin -u admin           # the password is prompted twice
python3 main.py                                 # http://127.0.0.1:8080  — admin: /admin
python3 -m pytest
```

For a real connectivity test, put the Xray binary at `bin/xray` (Windows: `bin\xray.exe`). Without it, the pages still load and no checks run.

Other commands: `reset-password`, `set-setting KEY VALUE`, `get-setting KEY`, `backup-db -o FILE`.

## Troubleshooting

| Problem | What to do |
|---|---|
| No checks run | Admin dashboard, Xray section. If the state is `missing`, use menu item 3 (Update Xray core) or `--xray-zip`. |
| A server is marked rejected by Xray | The config is invalid. The error is on the server page. Fix the link or choose Try again. |
| Every server is Unknown | The monitor’s internet may be down (red banner on the dashboard), or Xray is restarting. |
| Telegram sends nothing | Telegram page, Send test message. The queue and the last error are on the dashboard. `auto` or `via_server` needs at least one online server. |
| Admin path forgotten | `sudo v2uptime`, then 8 (Service status). |
| Admin password forgotten | `sudo v2uptime`, then 10 (Reset admin password). |
| Logs | `journalctl -u v2uptime -f`, or `/opt/v2uptime/logs/app.log` (JSON, rotated). |
| certbot fails | Port 80 open, DNS correct, or use an existing certificate from menu item 6. |

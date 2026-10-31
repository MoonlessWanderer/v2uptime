# Changelog

## 1.2.0

- **Settings → Maintenance**: update and restart the panel from the admin panel. Update runs the same update as menu item 2 (download, new virtualenv, swap, health check, rollback), through a small root unit (`v2uptime-control.path`) that the installer sets up; the panel itself still has no root access. The page follows the update, shows its log, and reloads when the new version is up. Restart stops the panel and systemd starts it again.
- After updating an older installation, run `sudo v2uptime` once to set up the unit; the Maintenance page says so until then.
- `sudo v2uptime --setup-control` sets up the unit again. Uninstall removes it.

## 1.1.0

### Ping measures a request on an open connection

Each check now sends two requests over one keep-alive connection. The first (cold) request decides success and its time is stored as `connect_ms` (connect time: tunnel, TLS or REALITY, and the connection to the test host). The second (warm) request is the ping (`latency_ms`), the round trip of one request through the server, the same idea as the delay in 3x-ui. Before this release the ping was the cold request time, roughly 3 to 5 times a real round trip. The reference check is still one cold request.

- The public list shows the median ping of the successful checks in the last 10 minutes.
- The admin server page shows the 24h average connect time. **Test now** shows both numbers.
- `sudo v2uptime probe ID [--count N]` prints cold and warm times with connection phases.
- Existing data is not converted. Checks stored before the update hold cold times, so the ping chart drops when you update, and for up to 24 hours jitter is slightly high and the stability score slightly low. No migration is needed; the new `checks.connect_ms` column is added on start.

### Names

- Server names are shown exactly as written everywhere. The flag that was added in front of the name is gone, so a name with a flag no longer shows it twice.
- API: the `flag` field is removed from `/api/status` and `/api/servers/{id}/history`. `country` stays.
- The country is read from the last complete flag in the name (`🇮🇷➡️🇩🇪 Tunnel` → `DE`). Lone regional-indicator letters are ignored.
- Chrome and Edge on Windows show flags inside names with a bundled font (Twemoji, CC BY 4.0), loaded only when the browser cannot draw flags.

### Other fixes

- Durations: `1h` instead of `1h 1s`, `1d` instead of `1d 1s`; the second unit is shown only when it is the next smaller unit and not zero.
- Bucket labels that end at midnight no longer repeat the date (`2026-10-30 23:30–00:00`).
- `status_label` is `New` in the API for servers that are still collecting data, as on the page.
- Telegram recovery and network-restore messages include dates when the outage crosses midnight.
- Public page: the status banner is above the sort tabs; mobile columns line up in every row.

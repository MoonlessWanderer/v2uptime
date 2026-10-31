# Changelog

## Unreleased

## 1.4.4

- The admin server table fits at 1280px. Row actions are icons, the grip moves a row with the arrow keys, and the checkbox, grip, and name stay visible while the rest of the table scrolls.
- Five failures from one IP no longer lock the admin username. That limit is 50 failures in 15 minutes, and an IP that signed in during the last 30 days is exempt. Changing the password forgets those IPs.
- Numbers on the public ping chart are drawn on top of the line.
- The public page no longer shows a test location or a footer. The last-check time still carries the timezone in its tooltip. Test location remains in Telegram reports.

## 1.4.3

- An open 1h cell is labeled with its own minute, the same clock as the closed card. Minutes with no check after the last one are left out. A failure or an unknown result keeps its minute.

## 1.4.2

- An open 1h chart no longer adds an empty point for the minute that has not been checked yet. A check that already landed in the current minute still shows.
- Fastest and Most stable keep the same text size when selected. The active tab is only a different background.
- A server that was just added sorts on Fastest as soon as it has a ping, and on Most stable once two checks can measure jitter.

## 1.4.1

- The public page no longer has a server search box. Sorting stays.

## 1.4.0

- An open card starts on 1h, and the same control has 24h, 7d, and 30d. On every range the chart point is the same cell as the status bar. A 1h or 24h point's ping is the median of that cell; a 7d or 30d point's ping is the average of that cell. The tooltip shows that cell.
- A closed card shows the last 60 checks, one cell each, labelled 1h. Online, Degraded, and Down no longer include the uptime percent on that line.
- An update no longer crash-loops when `servers.subscription_id` still references a `subscriptions` table that is already gone. The column is removed by rebuilding `servers`, and the servers and their history stay.
- The public page and the admin panel use Vazirmatn at two weights (400 and 600) and one size scale. Sort tabs no longer recolor ping or uptime.
- Persian numbers on the public page keep their order. An open card shows the average ping as `260 ms`, and the footer timezone, parentheses included, stays one left-to-right run. Resolving a subscription link no longer blocks the check loop.
- The public status page can be shown in English or Persian. Only the Public page language setting chooses it. Dates and digits stay as they were.
- Config domains can be resolved with a primary and a secondary DNS (Settings → Monitoring). Fake answers in `10.10.34.0/24` are dropped. A failed check of a domain server is stored as DNS error, or DNS blocked when only a filtered answer comes back. This needs Xray 25.6.8 or newer.
- Stored subscriptions are gone. Servers that came from one stay, as ordinary servers. A subscription URL in the add form is fetched once and its configs are saved like any other server; the URL is not kept and is not synced. The server list can make the selected rows public or hidden, or delete them and their history.
- A mouse click on the closed card's bar opens the card. A touch on that bar still only moves the tooltip. The bar and chart inside an open card do not close it.
- "Down for" omits seconds once the outage is a minute or longer (`47m`, not `47m 2s`). Other durations, including "lasted", are unchanged. "Down since" already uses minute-level relative time.
- An update writes the database backup into a directory owned by the service user. If that backup fails, the update log says so and the stopped database is copied with its WAL files. A failed update restores that copy before the previous version starts.

## 1.3.0

### Public page: one card per server

- The status banner is gone; its text stays for screen readers only (`#live-summary`, `aria-live`). "Last check" moved under the last card.
- Each server is a card: name and ping, the 24h status bar, then status and 24h uptime (`Online · 100% uptime`, `Down for 1h · 96.73% uptime`, `New · Collecting data`). The whole card opens and closes; the bar does not.
- An open card has one 24h / 7d / 30d control for its uptime, average ping, incident count, status bar, and ping chart, all from one request. The bar and the chart share a time axis, and a mouse or finger on either shows a cursor line with one tooltip for that slot (time, uptime, ping). The 24h bar of the closed card is hidden while the card is open.
- Tooltips work with touch: the slot follows the finger along the bar or chart, and the page still scrolls vertically.
- The 60-second refresh keeps open cards, their range, the sort tab, the scroll position, and keyboard focus, and refreshes the open card's data.

### API

- `/api/servers/{id}/history` has a new `bars` field (the status bar for the range, same cells as the status page). Nothing was removed or renamed.

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

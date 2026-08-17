# DLNA Web Controller

A browser-based control panel for the DLNA/UPnP testbed, built on top of the
existing `dlnarenderer.py` / `dlnabrowser.py` / `playqueue.py` logic. Runs as
a small Flask service -- point a browser at it from any device on your LAN
(phone, laptop, another Pi) and you get:

- A **browse panel** for your media server's folder tree, indexed at startup
  so navigating it is instant. Click a track's title to queue it, or use
  "Queue All"/the per-folder "Queue N Tracks" button to queue everything in
  a folder at once (capped at `QUEUE_TRACK_LIMIT` tracks -- see "Queue size
  limit" below)
- A **queue panel** with click-to-play, remove-from-queue, next/prev/stop,
  and a live LED-style progress meter
- A **renderer picker** to discover and select the active output device,
  remembered across restarts. The media server itself is fixed at deploy
  time (see "Configuration" below), not chosen at runtime
- A **console panel** with live debug/status logging (also available at
  `GET /api/logs`)
- Graceful handling of renderers that are only sometimes on: selecting one
  that's off fails cleanly instead of crashing, a background check reports
  when it goes offline/comes back, and the app quietly retries a
  previously-selected renderer in the background until it appears.

## Configuration

The media server is set once, at deploy time, via an environment variable --
there's no in-app picker for it. This is deliberate: the app indexes the
server's entire folder tree at startup (see "Media library indexing"
below), so switching servers on the fly isn't a supported flow; changing it
means editing the env var and restarting the service.

```bash
export MEDIA_SERVER_DESC_URL=http://127.0.0.1:8200/rootDesc.xml
```

This needs to be the UPnP **description XML** URL, not just a host:port --
that document is where the app finds both the ContentDirectory control URL
(needed to browse anything) and the server's `<friendlyName>` (shown in the
UI), and its path isn't standardized across DLNA server software. Find
yours with:

```bash
curl http://<host>:<port>/rootDesc.xml
```

If that returns UPnP XML with a `<friendlyName>` tag, that's your URL. For
MiniDLNA specifically: the default path is `/rootDesc.xml`, and if it's
running on the same machine as this app, `127.0.0.1` is worth trying even
though MiniDLNA's docs say it excludes loopback by default -- in practice
its HTTP listener commonly binds `0.0.0.0` regardless, so it's worth
confirming with the `curl` above before assuming you need the LAN IP.
Loopback is the more robust choice if it works: unlike a LAN IP, it isn't
affected by DHCP reassignment or switching network interfaces.

## Running it

```bash
pip install -r requirements.txt
export MEDIA_SERVER_DESC_URL=http://127.0.0.1:8200/rootDesc.xml
python app.py
```

Then open `http://<pi-ip>:5000/` from any device on the same network. The
page will show an indexing screen for a bit at first startup (see below)
before the normal UI appears.

Useful environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `MEDIA_SERVER_DESC_URL` | *(required)* | The media server's UPnP description XML URL -- see "Configuration" above |
| `QUEUE_TRACK_LIMIT` | `200` | Max direct-child tracks a single "queue this folder" action may add at once -- see "Queue size limit" below |
| `DLNA_LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose SOAP/GENA/SSDP tracing in the console panel |
| `DLNA_WEB_PORT` | `5000` | Port to listen on |
| `DLNA_WEB_CONFIG` | `~/.dlna_web_config.json` | Where the last-selected renderer is remembered |

## Media library indexing

At startup, the app connects to the configured media server and crawls its
entire folder tree, one `Browse` call per folder -- the same lazy caching
`browse_container()` already did as you navigated, just done eagerly upfront
instead. Once it's done, browsing anywhere in the library is instant (no
SOAP round-trip) for the rest of the process's life, and this is also the
foundation the per-folder "Queue N Tracks" button (see below) reads from
without needing any further network calls.

**Until that crawl finishes, the whole app is gated** -- the page shows an
"Indexing your library..." screen with live progress (folders scanned,
tracks found) instead of the normal UI. This is intentional, not a bug: it
avoids the complexity of the app being half-functional (e.g., a folder that
looks empty because it hasn't been crawled yet, versus one that's actually
empty). For a ~10,000-track library on a Pi talking to a same-machine
MiniDLNA over loopback, this typically takes well under a minute, but
scales with your library's folder count, not track count directly (a
folder with 200 tracks costs the same one `Browse` call as a folder with 2).

If the configured media server isn't reachable yet at startup (e.g.
MiniDLNA hasn't finished starting), the app retries with backoff
indefinitely rather than giving up -- this is the expected case when
systemd starts things in parallel, not an error (see `dlna-web.service`'s
comments on `After=`, which only guarantees launch order, not readiness).

If `MEDIA_SERVER_DESC_URL` isn't set at all, that's a real configuration
error rather than something worth retrying -- the indexing screen shows it
clearly instead of retrying forever, and `/var/log/dlna-web.log`/the
console panel will show it too.

**The index doesn't auto-refresh.** If you add, remove, or rename files on
the media server, the running app won't notice -- restart the service to
re-crawl. There's no in-app "rescan" trigger (yet); the whole design here
assumes a crawl-once-at-boot model, matching how infrequently a home music
library actually changes.

## Queue size limit

`QUEUE_TRACK_LIMIT` (default 200) caps how many tracks a single "queue
this folder" action can add at once, counting only that folder's *direct*
children -- deliberately not recursive, since queueing an entire subtree
in one click is exactly the runaway-queueing scenario this exists to
prevent.

This matters more than it might look like it should, depending on your
media server's configuration. Some DLNA servers (MiniDLNA in particular)
expose database-driven views alongside the real folder tree by default --
an "All Music" container with literally every track as a direct child,
plus per-Artist, per-Album, and per-Genre views that are alternate paths
to the same files, not additional ones. Without this cap, "Queue All" (and the per-folder "Queue N Tracks" button)
would happily queue your *entire* library in one click if you ever
navigated into one of those. `DLNABrowser.count_tracks()` is what backs
the cap -- an in-memory count off the already-warmed cache, so checking it
costs nothing extra at request time.

The limit is enforced in two places, not just one: `api_queue_add_folder()`
in `app.py` checks it server-side and rejects with a clear error over the
limit, and the "Queue All" button's enabled/disabled state (with a
tooltip explaining why) is a client-side reflection of the same check --
the button being disabled is a UI nicety, not the actual guarantee, so a
stale page or a client bug can't be used to bypass it.

If you'd rather not have MiniDLNA present those extra views at all,
setting `root_container=B` in `minidlna.conf` restricts what it exposes
to just the real folder tree. That's a legitimate alternative to raising
or removing this cap, but not a substitute for it -- the cap protects
against *any* oversized folder, from any server, for any reason, not just
MiniDLNA's specific default views.

## Running it as a boot-time service (recommended for the Pi)

The command above uses Flask's built-in dev server, which is fine for
testing but explicitly not meant to be left running unattended (it says so
on startup). For a service that starts at boot and restarts itself if it
ever crashes, run it under **waitress** instead, managed by **systemd**.

Why waitress and not gunicorn: this app keeps one shared "currently
selected renderer/queue" in memory and streams live updates over
long-lived connections (SSE). That needs exactly one process handling
requests. Waitress's default model (one process, a pool of threads) is a
natural fit and installs with a plain `pip install` -- no compiler needed
on the Pi. Gunicorn's default multi-*process* model would give each
worker its own out-of-sync copy of the renderer state, so if you use
gunicorn instead, keep it to a single worker.

1. **Put the project somewhere permanent**, e.g. `/home/pi/dlna-web`
   (adjust the paths below if you use a different location or username).

2. **Create a virtual environment and install dependencies:**
   ```bash
   cd /home/pi/dlna-web
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```

3. **Test it manually first:**
   ```bash
   MEDIA_SERVER_DESC_URL=http://127.0.0.1:8200/rootDesc.xml \
     venv/bin/waitress-serve --host=0.0.0.0 --port=8080 app:app
   ```
   Open `http://<pi-ip>:8080/` and confirm it works, then `Ctrl+C`.

4. **Edit `dlna-web.service`** to match your setup -- it defaults to user
   `pi`, working directory `/home/pi/dlna-web`, port `8080`, and
   `MEDIA_SERVER_DESC_URL=http://127.0.0.1:8200/rootDesc.xml`. If MiniDLNA
   runs as its own systemd service on this Pi, also uncomment the
   `After=`/`Wants=minidlna.service` lines.

5. **Install and enable the service:**
   ```bash
   sudo cp dlna-web.service /etc/systemd/system/dlna-web.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now dlna-web
   ```

6. **Check it's running and watch logs:**
   ```bash
   sudo systemctl status dlna-web
   tail -f /var/log/dlna-web.log
   ```
   The app's own output (SOAP/GENA/SSDP tracing, etc.) goes to that log
   file now, not the journal -- see the comments above `StandardOutput=`
   in `dlna-web.service` for why (in short: `append:` avoids a
   long-documented bug where `file:` doesn't reliably truncate on
   restart). `journalctl -u dlna-web -f` still shows something -- just
   systemd's own start/stop/crash/restart messages for the unit, not the
   app's logging.

   That log file grows forever on its own; a simple logrotate config
   keeps it in check:
   ```
   # /etc/logrotate.d/dlna-web
   /var/log/dlna-web.log {
       daily
       rotate 7
       compress
       missingok
       notifempty
       copytruncate
   }
   ```
   `copytruncate` matters here specifically: `StandardOutput=append:`
   holds the file open for the service's entire lifetime, so a plain
   rotate-and-reopen (the logrotate default) would leave the app writing
   to a now-unlinked file forever until the next restart. `copytruncate`
   copies the current content out then truncates the original in place,
   which the still-open file descriptor tolerates correctly.

It'll now start automatically on every boot and restart itself if it ever
exits unexpectedly. To update the code later: stop the service, replace
the files, `sudo systemctl start dlna-web`.

## What changed vs. the CLI version

- `dlnarenderer.py` / `dlnabrowser.py`: same UPnP/SOAP/SSDP logic, but
  diagnostics go through `logging` instead of `print()`, and the interactive
  `input()`-driven selection flows now have non-interactive equivalents
  (`resolve_control_url()` was already non-interactive; `DLNABrowser.connect()`
  is new) that the web layer calls directly. `DLNABrowser.warm_cache()` is
  new too -- the startup crawl described above. The CLI (`controller.py`,
  `main.py`) still works unmodified if you want it -- both entry points share
  the same underlying modules.
- `playqueue.py`: unchanged behavior, but every mutation now publishes an
  event (`queue_status`, `now_playing`) so connected browser tabs update
  live via Server-Sent Events instead of polling.
- New files: `app.py` (routes), `state.py` (the renderer/browser/queue
  singleton + connectivity monitor + startup/indexing orchestration),
  `events.py` (pub/sub for SSE), `logging_setup.py`, `config.py`,
  `templates/index.html`, `static/*`.

## Architecture notes

- **Single active output.** Like the CLI, this controls one renderer at a
  time; every connected browser tab sees and controls the same queue. That
  was explicitly in scope (no multi-room) -- if that changes later, the
  `AppState` singleton is the place that would need to become per-session.
- **No SocketIO/eventlet.** Live updates use plain Server-Sent Events over
  a normal threaded Flask response, so the dependency list stays just
  `Flask` + `requests` -- easy to install on a Pi.
- **Renderer availability.** `state.py` runs a 1-second background tick that
  polls transport state; a failed poll means "offline," not an exception.
  Transitions (online -> offline and back) are logged and pushed to the UI.
  Playback commands (`play_uri`, `pause`, `stop`) all return `True`/`False`
  now instead of assuming success, so a failed command shows up as a log
  warning + a toast rather than silently doing nothing.
- **Media server availability.** Unlike the renderer, the configured media
  server isn't optional -- the whole app is gated behind successfully
  connecting to and indexing it (see "Media library indexing" above), with
  indefinite retry-with-backoff for the connection itself.
- **SSDP/GENA still need real LAN access** (for renderers). As discussed
  before writing any code: run this directly on the Pi's host network (no
  Docker NAT) since both discovery (SSDP multicast) and the GENA event
  callback need a real routable local IP.

## Known limitations / good next additions

- **No live library rescan.** As noted above, changes to the media
  server's files require restarting the service. A "Rescan" trigger
  (re-running `warm_cache()` on demand rather than only at startup) would
  be a natural addition if the library changes often enough for that to
  matter.
- No auth. Fine on a trusted home LAN; if you ever expose this beyond your
  LAN, put it behind a reverse proxy with auth in front.

## License

Copyright (c) 2026 Paul H. Breslin.

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation, either version 3 of the License, or (at
your option) any later version. See the LGPL header at the top of each
source file, or <https://www.gnu.org/licenses/>, for the full terms.

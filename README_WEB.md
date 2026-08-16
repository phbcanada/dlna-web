# DLNA Web Controller

A browser-based control panel for the DLNA/UPnP testbed, built on top of the
existing `dlnarenderer.py` / `dlnabrowser.py` / `playqueue.py` logic. Runs as
a small Flask service -- point a browser at it from any device on your LAN
(phone, laptop, another Pi) and you get:

- A **browse panel** for your media server's folder tree
- A **queue panel** with click-to-play, next/prev/stop, and a live LED-style
  progress meter
- A **device picker** to discover and select the active media server / output
  renderer, with settings remembered across restarts
- A **console panel** with live debug/status logging (also available at
  `GET /api/logs`)
- Graceful handling of renderers that are only sometimes on: selecting one
  that's off fails cleanly instead of crashing, a background check reports
  when it goes offline/comes back, and the app quietly retries a
  previously-selected renderer in the background until it appears.

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Then open `http://<pi-ip>:5000/` from any device on the same network.

Useful environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `DLNA_LOG_LEVEL` | `INFO` | Set to `DEBUG` for verbose SOAP/GENA/SSDP tracing in the console panel |
| `DLNA_WEB_PORT` | `5000` | Port to listen on |
| `DLNA_WEB_CONFIG` | `~/.dlna_web_config.json` | Where the last-selected server/renderer are remembered |

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
   venv/bin/waitress-serve --host=0.0.0.0 --port=8080 app:app
   ```
   Open `http://<pi-ip>:8080/` and confirm it works, then `Ctrl+C`.

4. **Edit `dlna-web.service`** to match your setup -- it defaults to user
   `pi`, working directory `/home/pi/dlna-web`, and port `8080`.

5. **Install and enable the service:**
   ```bash
   sudo cp dlna-web.service /etc/systemd/system/dlna-web.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now dlna-web
   ```

6. **Check it's running and watch logs:**
   ```bash
   sudo systemctl status dlna-web
   journalctl -u dlna-web -f
   ```

It'll now start automatically on every boot and restart itself if it ever
exits unexpectedly. To update the code later: stop the service, replace
the files, `sudo systemctl start dlna-web`.

## What changed vs. the CLI version

- `dlnarenderer.py` / `dlnabrowser.py`: same UPnP/SOAP/SSDP logic, but
  diagnostics go through `logging` instead of `print()`, and the interactive
  `input()`-driven selection flows now have non-interactive equivalents
  (`resolve_control_url()` was already non-interactive; `DLNABrowser.connect()`
  is new) that the web layer calls directly. The CLI (`controller.py`,
  `main.py`) still works unmodified if you want it -- both entry points share
  the same underlying modules.
- `playqueue.py`: unchanged behavior, but every mutation now publishes an
  event (`queue_status`, `now_playing`) so connected browser tabs update
  live via Server-Sent Events instead of polling.
- New files: `app.py` (routes), `state.py` (the renderer/browser/queue
  singleton + connectivity monitor), `events.py` (pub/sub for SSE),
  `logging_setup.py`, `config.py`, `templates/index.html`, `static/*`.

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
- **SSDP/GENA still need real LAN access.** As discussed before writing any
  code: run this directly on the Pi's host network (no Docker NAT) since
  both discovery (SSDP multicast) and the GENA event callback need a real
  routable local IP.

## Known limitations / good next additions

- No per-track "remove from queue" yet (the original `PlayQueue` didn't
  have one either) -- straightforward to add if useful: a `remove_at(index)`
  method plus a small DELETE route.
- `PlayQueue.shutdown()` (called when switching renderers) stops the polling
  loop but doesn't cleanly tear down a running GENA HTTP listener thread --
  harmless on a Pi that switches renderers rarely, but worth hardening if
  you'll be switching outputs often.
- No auth. Fine on a trusted home LAN; if you ever expose this beyond your
  LAN, put it behind a reverse proxy with auth in front.

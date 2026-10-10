# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

A self-hosted, browser-based DLNA/UPnP music controller (Flask) that runs on a Raspberry Pi. It browses a MiniDLNA media server and pushes tracks to one DLNA renderer at a time. It grew out of a CLI testbed (`main.py` / `controller.py`), which still works and shares the same core modules.

- Single developer: Paul H. Breslin. Home LAN only, no auth, **single output by design (no multi-room)**.
- Target renderers: **gmediarender** (on the Pi), a **GGMM M4** wireless speaker, and an **LG Smart TV**. The GGMM is the usual source of behavioral edge cases.
- `README.md` is detailed and accurate on features, deployment, and design rationale. Read the relevant section before changing queue limits, shuffle, indexing, or the systemd setup.

## Commands

```bash
# Dev run (Flask dev server, port 5000 by default)
pip install -r requirements.txt
export MEDIA_SERVER_DESC_URL=http://127.0.0.1:8200/rootDesc.xml
python app.py            # or: python app.py --port 8080

# Production-style run (what systemd uses)
venv/bin/waitress-serve --host=0.0.0.0 --port=8080 app:app

# Syntax check (standard first verification step)
python -m py_compile *.py

# On the Pi
sudo systemctl restart dlna-web
tail -f /var/log/dlna-web.log          # app logging (NOT the journal)
journalctl -u dlna-web -f              # systemd lifecycle messages only
```

Env vars: `MEDIA_SERVER_DESC_URL` (required), `QUEUE_TRACK_LIMIT` (200), `DLNA_LOG_LEVEL` (INFO; DEBUG gives SOAP/GENA/SSDP tracing), `DLNA_WEB_PORT` (5000), `DLNA_WEB_CONFIG` (`~/.dlna_web_config.json`).

There is no committed test suite and no linter config.

## Architecture

| File | Role |
|---|---|
| `app.py` | Flask routes, `/api/stream` SSE endpoint, library-ready gate (`before_request` returns 503 until indexing is done). Calls `state.startup()` at **module level** so it runs under waitress. |
| `state.py` | `AppState` singleton (`state`): renderer, browser, queue, session, config, volume capability, 1-second ticker, renderer-lost detection, startup/indexing orchestration with retry/backoff. |
| `playqueue.py` | `Queue` (renderer-independent data: tracks, `current_idx`, shuffle, M3U playlists in `~/.playlists`), `PlaybackSession` (renderer-bound: GENA listener on port 8089, polling fallback, transport commands, `handle_stopped_notification()` probe), and `PlayQueue` (thin backward-compatible facade for the CLI). |
| `dlnarenderer.py` | SSDP discovery and SOAP for AVTransport, RenderingControl (volume), ConnectionManager. |
| `dlnabrowser.py` | ContentDirectory browsing, startup crawl (`warm_cache()`), `count_tracks()`. |
| `events.py` | Thread-safe pub/sub bus backing SSE. Never blocks publishers. |
| `logging_setup.py` | Root logging plus broadcast handler (in-memory `LOG_BUFFER` + `log` SSE events). |
| `config.py` | JSON settings: `renderer_desc_url`, `active_playlist`. |
| `controller.py`, `main.py` | Original CLI. Keep it working; don't break the `PlayQueue` facade. |
| `templates/index.html`, `static/app.js`, `static/style.css` | Vanilla JS/CSS frontend. No build step, no framework. |
| `dlna-web.service` | systemd unit (waitress, `append:` log redirection; see comments for why). |

Key invariants:

- `state.queue` always exists and survives renderer changes. `state.session` is `None` when no renderer is attached. Transport routes must guard with `_require_session()`; queue-mutation routes don't need to.
- `select_renderer()` issues an unconditional `stop()` before creating a new session.
- Exactly one process must serve requests (shared in-memory state + SSE). Never use multi-process workers.
- The library is crawled once at boot. There is no rescan; adding or removing files means a restart.
- `QUEUE_TRACK_LIMIT` is enforced server-side in both `api_queue_add_folder()` and `api_queue_add_specific_folder()`. The disabled UI button is a convenience, not the guarantee.

## Code conventions (follow these consistently)

- **Renderer failure is expected, not exceptional.** SOAP and renderer calls return `None`/`False` instead of raising. Callers log and publish rather than crash.
- **Publish an SSE event after every state change** (`queue_status`, `now_playing`, `renderer_volume`, etc.) so all open tabs stay in sync.
- **Per-renderer capability detection happens at connection time.** Example: one `GetVolume` call in `_refresh_volume_capability()` decides whether volume is supported.
- **Graceful degradation is a first-class requirement.** The LG TV returns UPnPError 606 on volume, so its volume control is hidden or disabled rather than treated as an error (`_require_volume_support()`).
- **Flask routes use guard helpers** (`_require_session()`, `_require_volume_support()`) for preconditions.
- Every source file starts with the LGPLv3 header `Copyright (c) 2026 Paul H. Breslin`. Add it to any new file.
- Comments explain *why* (renderer quirks, race conditions, deployment gotchas). Match that style and keep existing rationale comments intact.
- Frontend: the volume slider sends only on `change` (release), never on `input`, to avoid out-of-order SSE echoes snapping it back. Keyboard navigation uses the `body.kbd-nav` mode class to avoid hover/selection conflicts.

## Hard-won renderer/protocol knowledge

- **GENA is unreliable in practice.** Polling fallbacks and confirmation probes are necessary for correctness.
- **gmediarender quirk:** on `SetAVTransportURI` immediately after a track ends, its GStreamer pipeline emits a real `PLAYING` and then a spurious `STOPPED` 10–25 ms later. The client code was correct; the renderer was the anomaly. This is why `handle_stopped_notification()` re-probes `GetTransportInfo` before advancing. That probe has been live-tested against the GGMM.
- **Artist metadata:** gmediarender doesn't reliably echo artist via `GetPositionInfo`. Browse-time DIDL metadata is authoritative.
- **MiniDLNA works over loopback** (`127.0.0.1:8200/rootDesc.xml`) despite its docs suggesting otherwise. Prefer loopback.
- SSDP and GENA need the Pi's real LAN interface. No Docker NAT.

## How Paul wants the work done

- **Evidence before fixes.** Don't propose or apply speculative behavioral fixes. Add diagnostic logging to confirm a hypothesis first, then change behavior. This is a firm rule.
- **Discuss before implementing non-trivial changes.** Explain the architectural rationale and flag design decisions that need his input instead of deciding them unilaterally.
- **Minimal, targeted diffs.** No broad refactors, no reformatting unrelated code, no swapping in large libraries or frameworks.
- **Don't state unverified claims as fact.** Distinguish clearly between what was observed or tested and what is inferred.
- **Verification calibration:**
  - Text, CSS, or doc changes: `py_compile` / syntax check only.
  - Logic changes: direct unit tests against a **fake renderer** (a stub object implementing the `DLNARenderer` methods used). Write these ad hoc; none are committed.
  - Playwright or live browser tests: **only** for timing-sensitive frontend/backend interactions where reading the code is genuinely insufficient. They almost always fail first time and are usually overhead.
- Real-renderer behavior can only be confirmed by Paul on his LAN. Say so when a change needs live testing, and say what to look for in `/var/log/dlna-web.log`.

## Open items (check status with Paul before acting)

- **Occasional renderer disconnects around midnight.** Cause unknown. Candidates: Pi scheduled timers, router maintenance windows, GGMM idle/standby. The next step is evidence (an overnight independent ping test or logging), not a fix.
- **Waitress SSE thread starvation.** Each open `/api/stream` holds a waitress thread permanently; the default pool is 4 and the service doesn't pass `--threads`. Raising it to 8 is a one-line change, but Paul wants SSE connection-count logging to confirm the problem first. Not urgent.
- **AirPlay output** has been researched (pyatv, cliraop/libraop, airbridge as reference). A small relay using cliraop as a subprocess looked tractable. No decision has been made.

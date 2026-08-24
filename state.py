# Copyright (c) 2026 Paul H. Breslin
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# state.py
#
# Single process-wide application state: the currently selected media
# server, the currently selected renderer, and the play queue bound to
# it. This is intentionally a singleton (not per-request/per-session) --
# the whole point is that everyone looking at the page sees and controls
# the same active output, same as the original single-terminal CLI did.
#
# The one property every method here is written around: a DLNA renderer
# is not always on. Selecting one, playing to it, or just checking on it
# can fail at any time, and that failure is expected, not exceptional.
# Nothing in this module should let a renderer being asleep/off/unplugged
# take the web app down with it.
import threading
import time
import os
import logging

from dlnarenderer import DLNARenderer
from dlnabrowser import DLNABrowser
from playqueue import Queue, PlaybackSession
from events import events
import config as cfgmod

logger = logging.getLogger("state")

# How often the background ticker polls the renderer for transport state +
# position. 1s matches the granularity of the original CLI's "Monitor Live
# Playback Progress" mode, so the web progress bar feels equally live.
TICK_INTERVAL_SECONDS = 1.0

# Transport states that mean "something is actually loaded on the
# renderer" -- position/duration/title are only meaningful in these
# states. Many renderers don't reset GetPositionInfo on Stop (some keep
# reporting the last track's title and position == duration indefinitely
# until something new is loaded), so once the state drops out of this set
# we deliberately stop trusting that leftover data rather than display it.
ACTIVE_TRANSPORT_STATES = {"PLAYING", "TRANSITIONING", "PAUSED_PLAYBACK"}

IDLE_POSITION = {"title": "None", "artist": None, "duration": "00:00:00", "position": "00:00:00"}

# How long we wait between attempts to reconnect to a saved-but-currently-
# unavailable renderer at startup.
RETRY_INTERVAL_SECONDS = 10.0

# How long the ticker tolerates a *selected* renderer being unreachable
# before giving up on it entirely. Brief unreachability (a TV's network
# stack napping for a few seconds, a momentary Wi-Fi hiccup) is normal and
# should NOT force a reselect -- that's what the reachable/unreachable
# toggle above this already handles gracefully. But a renderer that's been
# unreachable this long is presumed gone (powered off, taken off the LAN,
# etc.); at that point continuing to poll it every second forever is just
# noise, and the person should pick a renderer from the SSDP list again
# rather than have the app wait indefinitely for a device that may never
# come back.
RENDERER_LOST_TIMEOUT_SECONDS = 20.0

# The media server is a fixed, deploy-time value now (not chosen at
# runtime) -- set this once in the environment the service runs under.
MEDIA_SERVER_ENV_VAR = "MEDIA_SERVER_DESC_URL"

# Backoff for retrying the initial connection to the configured media
# server at startup -- systemd's After= only guarantees MiniDLNA has been
# *launched*, not that it's actually answering requests yet, so a few
# retries here is the normal case, not an error condition.
SERVER_RETRY_INITIAL_DELAY = 2.0
SERVER_RETRY_MAX_DELAY = 30.0


class AppState:
    def __init__(self):
        self.renderer = DLNARenderer()
        self.browser = DLNABrowser()

        # The queue is long-lived and renderer-independent -- created
        # once here and never replaced. Browsing, queueing, reordering,
        # and playlist management all work against this regardless of
        # whether a renderer is currently attached. Only `session`
        # (below) comes and goes with renderer selection/loss.
        self.queue = Queue()

        # The active renderer session (GENA/polling + transport
        # commands). None whenever no renderer is selected/reachable --
        # routes that need to actually push playback to a device should
        # guard on this (see app.py's _require_session()), while queue
        # mutation routes don't need to guard on anything.
        self.session = None

        self.config = cfgmod.load_config()

        self.renderer_connected = False
        self.renderer_last_seen = None
        self.server_connected = False

        # Per-renderer volume capability + current value. Re-probed once,
        # in select_renderer() -> _refresh_volume_capability(), whenever
        # the active renderer changes -- see that method's docstring for
        # why a single GetVolume call doubles as the capability check.
        self.volume_supported = False
        self.volume = None

        # Library readiness -- all-or-nothing gate. Nothing that depends on
        # the media server (browsing, queueing) is usable until this is
        # True; see app.py's before_request gate.
        self.library_ready = False
        self.library_error = None
        self.library_index_progress = {"folders_scanned": 0, "tracks_found": 0}

        self._ticker_thread = None
        self._ticker_stop = threading.Event()
        self._state_lock = threading.Lock()

        # Timestamp of when the currently-selected renderer first became
        # unreachable, or None while it's reachable (or nothing's
        # selected). Reset the moment it's reachable again -- only a
        # *continuous* stretch of unreachability counts toward giving up
        # on it; see RENDERER_LOST_TIMEOUT_SECONDS.
        self._unreachable_since = None

    # ------------------------------------------------------------------
    # Renderer
    # ------------------------------------------------------------------

    def discover_renderers(self, timeout=3):
        urls = DLNARenderer.discover_renderers(timeout=timeout)
        results = []
        for url in urls:
            name = DLNARenderer.get_friendly_name(url)
            results.append({"desc_url": url, "friendly_name": name})
        return results

    def select_renderer(self, desc_url):
        """Connects to a renderer by its description URL. Raises on failure
        (caller/route turns that into a clean 502 for the UI) -- but a
        failure here just means "try again later", not "restart the app"."""
        new_renderer = DLNARenderer()
        new_renderer.resolve_control_url(desc_url)  # raises if unreachable

        with self._state_lock:
            if self.session:
                self.session.shutdown()
            self.renderer = new_renderer
            self.session = PlaybackSession(self.renderer, self.queue)
            self.session.start_monitoring()
            self.renderer_connected = True
            self.renderer_last_seen = time.time()

        # Per the agreed design: a (re)selected renderer always starts
        # from a clean slate, same as reaching the end of the queue --
        # no attempt to resume wherever the queue's current_idx happened
        # to be under a previous (or lost) session, and nothing here
        # auto-plays. The queue's contents themselves are untouched.
        self.queue.reset_position()

        self.config["renderer_desc_url"] = desc_url
        cfgmod.save_config(self.config)

        events.publish("renderer_status", {
            "connected": True,
            "friendly_name": self.renderer.friendly_name,
            "host": self.renderer.host,
        })
        logger.info(f"Connected to renderer: {self.renderer.friendly_name} ({self.renderer.host})")
        self._refresh_volume_capability()
        self._ensure_ticker()
        return True

    def _refresh_volume_capability(self):
        """Probes the just-selected renderer for volume support via a
        single GetVolume call -- this doubles as both the capability
        check and the slider's initial value, since UPnP has no separate
        "do you support this" query. A None back just means this
        renderer doesn't get a volume control (unreachable for it,
        missing the service entirely, or -- as seen on one real device
        during testing -- implementing RenderingControl but explicitly
        refusing the action): reported, not treated as an error."""
        vol = self.renderer.get_volume()
        self.volume_supported = vol is not None
        self.volume = vol
        if self.volume_supported:
            logger.info(f"Renderer '{self.renderer.friendly_name}' supports volume control (currently {vol}).")
        else:
            logger.info(f"Renderer '{self.renderer.friendly_name}' has no usable volume control.")
        events.publish("renderer_volume", self.volume_status_payload())

    def volume_status_payload(self):
        """JSON-safe snapshot of volume state -- shared by /api/status and
        the "renderer_volume" SSE event, same pattern as
        library_status_payload()."""
        return {"supported": self.volume_supported, "value": self.volume}

    def set_volume(self, value):
        """Sets volume on the active renderer. Returns True/False -- same
        graceful-failure pattern as the transport controls (a rejected
        or failed call, e.g. the renderer having just gone offline, is
        expected here, not exceptional)."""
        if not self.renderer or not self.volume_supported:
            return False
        value = max(0, min(100, int(value)))
        ok = self.renderer.set_volume(value)
        if ok:
            self.volume = value
            events.publish("renderer_volume", self.volume_status_payload())
        return ok

    # ------------------------------------------------------------------
    # Active playlist -- which playlist file the queue rows' "+" button
    # appends tracks to. Persisted so it survives a restart; distinct
    # from the play queue itself and from save_playlist/load_playlist,
    # which operate on the whole queue rather than one track at a time.
    # ------------------------------------------------------------------

    def set_active_playlist(self, name):
        self.config["active_playlist"] = name
        cfgmod.save_config(self.config)
        logger.info(f"Active playlist set to '{name}'.")
        events.publish("active_playlist", {"name": name})

    @property
    def active_playlist(self):
        return self.config.get("active_playlist")

    # ------------------------------------------------------------------
    # Media server -- fixed at deploy time via MEDIA_SERVER_DESC_URL, not
    # chosen at runtime. See _connect_configured_server() below.
    # ------------------------------------------------------------------

    def library_status_payload(self):
        """JSON-safe snapshot of library readiness -- shared by the
        /api/library_status route and the "library_status" SSE event."""
        return {
            "ready": self.library_ready,
            "error": self.library_error,
            "progress": dict(self.library_index_progress),
            "server": {
                "connected": self.server_connected,
                "friendly_name": self.browser.friendly_name if self.server_connected else None,
                "desc_url": self.browser.desc_url,
            },
        }

    def _connect_configured_server(self):
        """Connects to the deploy-time-configured media server, retrying
        with backoff until it succeeds. Runs on a background thread so
        Flask can serve the "indexing" page immediately rather than
        blocking startup on it. Never gives up on a transient failure --
        the service being started before MiniDLNA has finished coming up
        is the expected case, not an error, per systemd's After= only
        guaranteeing launch order, not readiness."""
        desc_url = os.environ.get(MEDIA_SERVER_ENV_VAR, "").strip()
        if not desc_url:
            msg = (
                f"{MEDIA_SERVER_ENV_VAR} is not set -- configure it and restart "
                f"the service. See dlna-web.service / README_WEB.md."
            )
            logger.error(msg)
            self.library_error = msg
            events.publish("library_status", self.library_status_payload())
            return False

        delay = SERVER_RETRY_INITIAL_DELAY
        attempt = 0
        while not self.server_connected:
            attempt += 1
            if self.browser.connect(desc_url):
                self.server_connected = True
                logger.info(f"Connected to configured media server: {self.browser.friendly_name} ({desc_url})")
                events.publish("library_status", self.library_status_payload())
                return True

            logger.warning(
                f"Media server not reachable yet (attempt {attempt}, {desc_url}). "
                f"Retrying in {delay:.0f}s -- this is expected if it's still starting up."
            )
            time.sleep(delay)
            delay = min(delay * 1.5, SERVER_RETRY_MAX_DELAY)

        return True

    def _warm_library_cache(self):
        """Crawls the entire folder tree so browsing/queueing is instant
        for the rest of the process's life (see DLNABrowser.warm_cache).
        Sets library_ready=True when done -- the gate app.py's
        before_request checks."""
        logger.info("Indexing media library (this can take a minute)...")
        t0 = time.time()

        def on_progress(folders_scanned, tracks_found):
            self.library_index_progress = {
                "folders_scanned": folders_scanned,
                "tracks_found": tracks_found,
            }
            events.publish("library_status", self.library_status_payload())

        folders, tracks = self.browser.warm_cache(progress_callback=on_progress)
        elapsed = time.time() - t0

        self.library_index_progress = {"folders_scanned": folders, "tracks_found": tracks}
        self.library_ready = True
        logger.info(f"Library index complete: {folders} folders, {tracks} tracks in {elapsed:.1f}s. Ready.")
        events.publish("library_status", self.library_status_payload())

    def _connect_and_index_library(self):
        """Background-thread target: connect to the configured server,
        then crawl it. Split from startup() so it can run without
        blocking Flask from serving the indexing page."""
        if self._connect_configured_server():
            self._warm_library_cache()

    # ------------------------------------------------------------------
    # Startup -- never blocks, never raises
    # ------------------------------------------------------------------

    def startup(self):
        """Called once at process startup. Kicks off, all in the
        background so Flask can start serving immediately:
          1. The connectivity/position ticker (independent of the library)
          2. Reconnecting to the last-used renderer, if any (unchanged
             behavior from before -- a renderer being off at boot is
             normal, not an error)
          3. Connecting to the configured media server and indexing it
             (new -- gates the rest of the UI via library_ready)
        """
        self._ensure_ticker()

        renderer_url = self.config.get("renderer_desc_url")
        if renderer_url:
            try:
                self.select_renderer(renderer_url)
                logger.info("Reconnected to saved renderer on startup.")
            except Exception as e:
                logger.info(
                    f"Saved renderer not available at startup ({e}). "
                    f"Will keep retrying in the background; select a different one anytime."
                )
                self._start_deferred_renderer_retry(renderer_url)

        threading.Thread(target=self._connect_and_index_library, daemon=True).start()

    def _start_deferred_renderer_retry(self, desc_url):
        """A renderer that's off at boot is the normal case for this app
        (per the brief: renderers aren't always on). Rather than give up,
        keep trying quietly in the background until it appears -- or until
        the person picks a different renderer, which naturally supersedes
        this loop since a new session/renderer gets installed."""
        def retry_loop():
            while not self._ticker_stop.is_set():
                time.sleep(RETRY_INTERVAL_SECONDS)
                if self.renderer_connected:
                    return  # something else (manual selection) already connected
                try:
                    self.select_renderer(desc_url)
                    logger.info("Reconnected to saved renderer after retrying in the background.")
                    return
                except Exception:
                    continue
        threading.Thread(target=retry_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # Background ticker: connectivity + live playback position
    # ------------------------------------------------------------------

    def get_display_position(self):
        """Returns what the UI should show for transport state / position /
        duration / title -- the single source of truth used by both the
        live ticker and GET /api/status, so a page reload right after
        playback stops shows the same clean state as the live view does.

        Only trusts the renderer's GetPositionInfo while it reports an
        ACTIVE_TRANSPORT_STATE. Once playback has stopped (end of queue,
        an explicit Stop, or the renderer going idle), we return a clean
        idle readout instead of whatever position/title the device last
        happened to report -- some renderers don't clear that data on
        Stop, which otherwise leaves the UI showing the previous track's
        title with a fully-filled progress bar even though nothing is
        playing.

        Artist is the one field NOT trusted from the renderer -- not
        every renderer echoes it back in TrackMetaData (gmediarender in
        particular doesn't), so it's overlaid here from the play queue's
        own browse-time library metadata for whatever track is current,
        which is reliably populated since it comes straight from the
        media server's DIDL-Lite listing."""
        renderer = self.renderer
        if not renderer or not renderer.control_url:
            return {"transport_state": "NO_MEDIA_PRESENT", **IDLE_POSITION}

        transport_state = renderer.get_transport_state()
        if transport_state in ACTIVE_TRANSPORT_STATES:
            pos = dict(renderer.get_position_info())
            current = self.queue.current_track()
            if current and current.get("artist"):
                pos["artist"] = current["artist"]
        else:
            pos = dict(IDLE_POSITION)
        pos["transport_state"] = transport_state
        return pos

    def _ensure_ticker(self):
        if self._ticker_thread and self._ticker_thread.is_alive():
            return
        self._ticker_stop.clear()
        self._ticker_thread = threading.Thread(target=self._ticker_loop, daemon=True)
        self._ticker_thread.start()

    def _ticker_loop(self):
        while not self._ticker_stop.is_set():
            renderer = self.renderer
            if renderer and renderer.control_url:
                display = self.get_display_position()
                transport_state = display["transport_state"]
                reachable = transport_state != "UNKNOWN"

                if reachable != self.renderer_connected:
                    self.renderer_connected = reachable
                    if reachable:
                        logger.info(f"Renderer '{renderer.friendly_name}' is reachable again.")
                    else:
                        logger.warning(f"Renderer '{renderer.friendly_name}' stopped responding (offline or asleep).")
                    events.publish("renderer_status", {
                        "connected": reachable,
                        "friendly_name": renderer.friendly_name,
                        "host": renderer.host,
                    })

                if reachable:
                    self.renderer_last_seen = time.time()
                    self._unreachable_since = None
                else:
                    now = time.time()
                    if self._unreachable_since is None:
                        self._unreachable_since = now
                    elif now - self._unreachable_since >= RENDERER_LOST_TIMEOUT_SECONDS:
                        self._declare_renderer_lost(renderer)
                        continue  # self.renderer has changed; re-read next iteration

                events.publish("playback_tick", {
                    "connected": reachable,
                    "transport_state": transport_state,
                    "position": display.get("position"),
                    "duration": display.get("duration"),
                    "title": display.get("title"),
                    "artist": display.get("artist"),
                })
            time.sleep(TICK_INTERVAL_SECONDS)

    def _declare_renderer_lost(self, renderer):
        """Called from the ticker once a selected renderer has been
        continuously unreachable for RENDERER_LOST_TIMEOUT_SECONDS. Gives
        up on it entirely rather than polling forever: tears down its
        PlaybackSession (which also stops that renderer's own GENA/polling
        background threads -- see PlaybackSession.shutdown()), clears it
        as the active renderer so the ticker goes idle instead of
        continuing to hammer a dead host, resets the queue's playback
        position (see Queue.reset_position()), and tells the UI to send
        the person back to the renderer picker. The queue's *contents*
        are untouched -- only position resets. Deliberately does NOT
        touch the saved config entry -- that's only rewritten on an
        explicit new selection, so if this same renderer comes back
        later it's still one click away in the picker rather than gone
        from history."""
        logger.warning(
            f"Renderer '{renderer.friendly_name}' has been unreachable for "
            f"{RENDERER_LOST_TIMEOUT_SECONDS:.0f}s -- giving up on it. "
            f"Select a renderer from the list to resume."
        )
        with self._state_lock:
            if self.session:
                self.session.shutdown()
                self.session = None
            self.renderer = DLNARenderer()
            self.renderer_connected = False
            self._unreachable_since = None

        # Same clean-slate reset as a fresh renderer selection -- see
        # select_renderer(). Queue contents survive; playback position
        # does not carry over to whatever renderer comes next.
        self.queue.reset_position()

        events.publish("renderer_lost", {
            "friendly_name": renderer.friendly_name,
            "host": renderer.host,
        })


# Single process-wide instance, imported by app.py's routes.
state = AppState()

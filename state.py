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
import logging

from dlnarenderer import DLNARenderer
from dlnabrowser import DLNABrowser
from playqueue import PlayQueue
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

IDLE_POSITION = {"title": "None", "duration": "00:00:00", "position": "00:00:00"}

# How long we wait between attempts to reconnect to a saved-but-currently-
# unavailable renderer at startup.
RETRY_INTERVAL_SECONDS = 10.0


class AppState:
    def __init__(self):
        self.renderer = DLNARenderer()
        self.browser = DLNABrowser()
        self.queue = None  # created once a renderer is selected

        self.config = cfgmod.load_config()

        self.renderer_connected = False
        self.renderer_last_seen = None
        self.server_connected = False

        self._ticker_thread = None
        self._ticker_stop = threading.Event()
        self._state_lock = threading.Lock()

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
            if self.queue:
                self.queue.shutdown()
            self.renderer = new_renderer
            self.queue = PlayQueue(self.renderer)
            self.queue.start_monitoring()
            self.renderer_connected = True
            self.renderer_last_seen = time.time()

        self.config["renderer_desc_url"] = desc_url
        cfgmod.save_config(self.config)

        events.publish("renderer_status", {
            "connected": True,
            "friendly_name": self.renderer.friendly_name,
            "host": self.renderer.host,
        })
        logger.info(f"Connected to renderer: {self.renderer.friendly_name} ({self.renderer.host})")
        self._ensure_ticker()
        return True

    # ------------------------------------------------------------------
    # Media server
    # ------------------------------------------------------------------

    def discover_servers(self, timeout=3):
        urls = DLNABrowser.discover_servers(timeout=timeout)
        results = []
        for url in urls:
            name = DLNABrowser.get_friendly_name(url)
            results.append({"desc_url": url, "friendly_name": name})
        return results

    def select_server(self, desc_url):
        ok = self.browser.connect(desc_url)
        self.server_connected = ok
        events.publish("server_status", {
            "connected": ok,
            "desc_url": desc_url,
            "friendly_name": self.browser.friendly_name if ok else None,
        })
        if ok:
            self.config["server_desc_url"] = desc_url
            cfgmod.save_config(self.config)
            logger.info(f"Connected to media server: {self.browser.friendly_name} ({desc_url})")
        else:
            logger.warning(f"Could not connect to media server: {desc_url}")
        return ok

    # ------------------------------------------------------------------
    # Startup reconnection -- never blocks, never raises
    # ------------------------------------------------------------------

    def try_reconnect_defaults(self):
        """Called once at process startup. Attempts to reconnect to the
        previously selected server/renderer. Neither being unavailable
        should stop the app from starting -- the UI will just show
        'not connected' and let the person pick (or wait for) one."""
        server_url = self.config.get("server_desc_url")
        renderer_url = self.config.get("renderer_desc_url")

        if server_url:
            try:
                self.select_server(server_url)
            except Exception as e:
                logger.warning(f"Could not reconnect to saved media server on startup: {e}")

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

        self._ensure_ticker()

    def _start_deferred_renderer_retry(self, desc_url):
        """A renderer that's off at boot is the normal case for this app
        (per the brief: renderers aren't always on). Rather than give up,
        keep trying quietly in the background until it appears -- or until
        the person picks a different renderer, which naturally supersedes
        this loop since a new PlayQueue/renderer gets installed."""
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
        playing."""
        renderer = self.renderer
        if not renderer or not renderer.control_url:
            return {"transport_state": "NO_MEDIA_PRESENT", **IDLE_POSITION}

        transport_state = renderer.get_transport_state()
        if transport_state in ACTIVE_TRANSPORT_STATES:
            pos = dict(renderer.get_position_info())
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

                events.publish("playback_tick", {
                    "connected": reachable,
                    "transport_state": transport_state,
                    "position": display.get("position"),
                    "duration": display.get("duration"),
                    "title": display.get("title"),
                })
            time.sleep(TICK_INTERVAL_SECONDS)


# Single process-wide instance, imported by app.py's routes.
state = AppState()

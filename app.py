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
# app.py
#
# Entry point for the DLNA web controller.
#
# For local testing, run it directly (uses Flask's built-in dev server):
#     python app.py
#     python app.py --port 8080
#
# For a long-running/boot-time deployment, run it under a proper WSGI
# server instead -- e.g. waitress -- pointed at the `app` object below:
#     waitress-serve --host=0.0.0.0 --port=8080 app:app
# See dlna-web.service for a systemd unit that does this automatically.
#
# IMPORTANT: this app keeps a single shared "currently selected
# renderer/queue" in memory (see state.py) and streams live updates over
# long-lived SSE connections. Both of those need exactly ONE process
# handling requests. Waitress's default thread-pool-in-one-process model
# is a natural fit; don't run this under multiple worker *processes*
# (e.g. gunicorn with -w > 1) -- that would give each worker its own,
# out-of-sync copy of the renderer state.
#
# Environment variables:
#   MEDIA_SERVER_DESC_URL  required -- UPnP description XML URL of the
#                          media server to use (e.g.
#                          http://127.0.0.1:8200/rootDesc.xml). Fixed at
#                          deploy time; there's no runtime picker for this
#                          anymore -- see state.py's startup().
#   QUEUE_TRACK_LIMIT      max direct-child tracks a single "queue this
#                          folder" action may add at once (default 200).
#                          Deliberately not recursive -- see
#                          DLNABrowser.count_tracks(). Enforced server-side
#                          in api_queue_add_folder(), not just hidden in
#                          the UI.
#   DLNA_LOG_LEVEL   DEBUG / INFO / WARNING (default INFO)
#   DLNA_WEB_PORT    port for the `python app.py` dev-server path (default 5000)
#   DLNA_WEB_CONFIG  path to the settings JSON file (default ~/.dlna_web_config.json)
import argparse
import json
import logging
import os

from flask import Flask, jsonify, request, Response, render_template

from logging_setup import setup_logging, LOG_BUFFER
from events import events
from state import state

setup_logging()
logger = logging.getLogger("webapp")

app = Flask(__name__)

# Applies to both "Queue All" (the current folder) and the future
# per-folder "Queue N Tracks" buttons -- deliberately a hard cap, not a
# warning, since the whole point is that no single click should be able
# to queue an unbounded number of tracks (e.g. a media-server view like
# MiniDLNA's "All Music" that flattens the entire library into one
# folder's direct children).
QUEUE_TRACK_LIMIT = int(os.environ.get("QUEUE_TRACK_LIMIT", "200"))

# Routes reachable even while the library is still indexing -- everything
# else is blocked (503) until state.library_ready, per the all-or-nothing
# startup gate. Keep this list to exactly what the indexing screen itself
# needs: the page shell, its static assets, and the readiness/log/stream
# endpoints that drive its progress display.
_READY_GATE_ALLOWLIST = {"/", "/api/library_status", "/api/logs", "/api/stream"}


@app.before_request
def _gate_until_library_ready():
    if state.library_ready:
        return None
    path = request.path
    if path in _READY_GATE_ALLOWLIST or path.startswith("/static/"):
        return None
    return jsonify({
        "error": "Still indexing the media library -- try again shortly.",
        "library_status": state.library_status_payload(),
    }), 503


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# ----------------------------------------------------------------------
# Library readiness (indexing progress)
# ----------------------------------------------------------------------

@app.route("/api/library_status")
def api_library_status():
    return jsonify(state.library_status_payload())


# ----------------------------------------------------------------------
# Overall status
# ----------------------------------------------------------------------

@app.route("/api/status")
def api_status():
    q_snapshot = state.queue.snapshot() if state.queue else {"queue": [], "current_idx": -1}
    position = None
    if state.renderer and state.renderer.control_url:
        # Shared with the live ticker (see state.get_display_position) so a
        # page reload right after playback stops shows the same clean idle
        # readout as the live view, not the renderer's stale leftover data.
        position = state.get_display_position()

    return jsonify({
        "server": {
            "connected": state.server_connected,
            "desc_url": state.browser.desc_url,
            "friendly_name": state.browser.friendly_name if state.browser else None,
        },
        "renderer": {
            "connected": state.renderer_connected,
            "friendly_name": state.renderer.friendly_name if state.renderer else None,
            "host": state.renderer.host if state.renderer else None,
            "desc_url": state.renderer.desc_url if state.renderer else None,
        },
        "queue": q_snapshot,
        "position": position,
    })


# ----------------------------------------------------------------------
# Renderers
# ----------------------------------------------------------------------

@app.route("/api/renderers/discover")
def api_discover_renderers():
    timeout = int(request.args.get("timeout", 3))
    try:
        renderers = state.discover_renderers(timeout=timeout)
        return jsonify({"renderers": renderers})
    except Exception as e:
        logger.error(f"Renderer discovery failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/renderers/select", methods=["POST"])
def api_select_renderer():
    data = request.get_json(force=True, silent=True) or {}
    desc_url = data.get("desc_url")
    if not desc_url:
        return jsonify({"error": "desc_url is required"}), 400
    try:
        state.select_renderer(desc_url)
        return jsonify({
            "connected": True,
            "friendly_name": state.renderer.friendly_name,
            "host": state.renderer.host,
        })
    except Exception as e:
        return jsonify({"connected": False, "error": f"Renderer not reachable: {e}"}), 502


# ----------------------------------------------------------------------
# Browsing
# ----------------------------------------------------------------------

def _browse_payload(items):
    out_items = []
    file_count = 0
    for item_type, data in items:
        entry = {"type": item_type, "id": data.get("id"), "title": data.get("title")}
        if item_type == "file":
            entry["uri"] = data.get("uri")
            entry["media_type"] = data.get("media_type", "other")
            file_count += 1
        elif item_type == "folder":
            # Direct-child count only (not recursive) -- an in-memory
            # lookup off the already-warmed cache, no extra network call.
            # Drives the per-folder "Queue N Tracks" button: the frontend
            # hides it entirely above queue_track_limit, per the same cap
            # api_queue_add_folder()/api_queue_add_specific_folder() below
            # enforce for real.
            entry["track_count"] = state.browser.count_tracks(data["id"])
        out_items.append(entry)
    breadcrumb = [{"id": cid, "title": t} for cid, t in state.browser.history]
    breadcrumb.append({"id": state.browser.current_id, "title": state.browser.current_title})
    return {
        "items": out_items,
        "breadcrumb": breadcrumb,
        "can_go_back": bool(state.browser.history),
        # Lets the UI decide the "Queue All" button's state (and the
        # per-folder "Queue N Tracks" buttons') without a second request --
        # same cap enforced for real in the two add-folder routes below.
        "file_count": file_count,
        "queue_track_limit": QUEUE_TRACK_LIMIT,
    }


@app.route("/api/browse/current")
def api_browse_current():
    if not state.server_connected:
        return jsonify({"error": "No media server connected."}), 409
    items = state.browser.browse_container(state.browser.current_id)
    return jsonify(_browse_payload(items))


@app.route("/api/browse/enter", methods=["POST"])
def api_browse_enter():
    if not state.server_connected:
        return jsonify({"error": "No media server connected."}), 409
    data = request.get_json(force=True, silent=True) or {}
    container_id = data.get("id")
    title = data.get("title", "")
    if container_id is None:
        return jsonify({"error": "id is required"}), 400
    state.browser.history.append((state.browser.current_id, state.browser.current_title))
    state.browser.current_id = container_id
    state.browser.current_title = title
    items = state.browser.browse_container(container_id)
    return jsonify(_browse_payload(items))


@app.route("/api/browse/back", methods=["POST"])
def api_browse_back():
    if not state.server_connected:
        return jsonify({"error": "No media server connected."}), 409
    if state.browser.history:
        state.browser.current_id, state.browser.current_title = state.browser.history.pop()
    items = state.browser.browse_container(state.browser.current_id)
    return jsonify(_browse_payload(items))


# ----------------------------------------------------------------------
# Queue
# ----------------------------------------------------------------------

def _require_queue():
    if not state.queue:
        return jsonify({"error": "No renderer selected yet."}), 409
    return None


@app.route("/api/queue")
def api_queue():
    if not state.queue:
        return jsonify({"queue": [], "current_idx": -1})
    return jsonify(state.queue.snapshot())


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    err = _require_queue()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    title, uri = data.get("title"), data.get("uri")
    if not title or not uri:
        return jsonify({"error": "title and uri are required"}), 400
    item = {"id": data.get("id"), "title": title, "uri": uri}
    item["relative_path"] = state.browser._get_relative_path(title, uri)
    state.queue.add_to_queue(item)
    return jsonify(state.queue.snapshot())


@app.route("/api/queue/add_current_folder", methods=["POST"])
def api_queue_add_folder():
    err = _require_queue()
    if err:
        return err
    items = state.browser.browse_container(state.browser.current_id)
    file_items = [(t, d) for t, d in items if t == "file"]

    # Enforced here, not just via the button being disabled client-side --
    # a stale page, a browser back button, or a client bug shouldn't be
    # able to bypass the cap. Same limit DLNABrowser.count_tracks() and
    # the "Queue All" button's own state are based on.
    if len(file_items) > QUEUE_TRACK_LIMIT:
        return jsonify({
            "error": (
                f"This folder has {len(file_items)} tracks, which exceeds "
                f"the {QUEUE_TRACK_LIMIT}-track queue limit."
            )
        }), 400

    count = 0
    for item_type, data in file_items:
        entry = dict(data)
        entry["relative_path"] = state.browser._get_relative_path(entry["title"], entry["uri"])
        state.queue.add_to_queue(entry)
        count += 1
    return jsonify({"added": count, **state.queue.snapshot()})


@app.route("/api/queue/add_folder", methods=["POST"])
def api_queue_add_specific_folder():
    """Queues a folder's direct-child tracks by ID -- used by the
    per-folder "Queue N Tracks" button in a listing, which queues a
    *child* row without navigating the browse panel into it. Distinct
    from api_queue_add_folder() above (the "Queue All" button, which
    always means the currently-browsed folder)."""
    err = _require_queue()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    folder_id = data.get("id")
    folder_title = data.get("title", "")
    if folder_id is None:
        return jsonify({"error": "id is required"}), 400

    items = state.browser.browse_container(folder_id)
    file_items = [(t, d) for t, d in items if t == "file"]

    if not file_items:
        return jsonify({"error": "This folder has no tracks."}), 400
    if len(file_items) > QUEUE_TRACK_LIMIT:
        return jsonify({
            "error": (
                f"This folder has {len(file_items)} tracks, which exceeds "
                f"the {QUEUE_TRACK_LIMIT}-track queue limit."
            )
        }), 400

    # _get_relative_path() builds its path from self.history/current_title
    # -- i.e. wherever the browser is *currently* positioned. To get
    # correct paths for a folder we're deliberately NOT navigating to,
    # step into it just long enough to compute them, then restore the
    # real position exactly -- the browse panel shouldn't visibly move.
    saved_history = list(state.browser.history)
    saved_current_id = state.browser.current_id
    saved_current_title = state.browser.current_title
    try:
        state.browser.history.append((saved_current_id, saved_current_title))
        state.browser.current_id = folder_id
        state.browser.current_title = folder_title
        count = 0
        for item_type, item_data in file_items:
            entry = dict(item_data)
            entry["relative_path"] = state.browser._get_relative_path(entry["title"], entry["uri"])
            state.queue.add_to_queue(entry)
            count += 1
    finally:
        state.browser.history = saved_history
        state.browser.current_id = saved_current_id
        state.browser.current_title = saved_current_title

    return jsonify({"added": count, **state.queue.snapshot()})


@app.route("/api/queue/play", methods=["POST"])
def api_queue_play():
    err = _require_queue()
    if err:
        return err
    state.queue.play()
    return jsonify(state.queue.snapshot())


@app.route("/api/queue/play_at", methods=["POST"])
def api_queue_play_at():
    err = _require_queue()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    idx = data.get("index")
    if idx is None:
        return jsonify({"error": "index is required"}), 400
    ok = state.queue.play_at(int(idx))
    return jsonify({"ok": ok, **state.queue.snapshot()})


@app.route("/api/queue/remove", methods=["POST"])
def api_queue_remove():
    err = _require_queue()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    idx = data.get("index")
    if idx is None:
        return jsonify({"error": "index is required"}), 400
    ok = state.queue.remove_at(int(idx))
    return jsonify({"ok": ok, **state.queue.snapshot()})


@app.route("/api/queue/toggle", methods=["POST"])
def api_queue_toggle():
    err = _require_queue()
    if err:
        return err
    state.queue.toggle_play()
    return jsonify(state.queue.snapshot())


@app.route("/api/queue/next", methods=["POST"])
def api_queue_next():
    err = _require_queue()
    if err:
        return err
    state.queue.next()
    return jsonify(state.queue.snapshot())


@app.route("/api/queue/prev", methods=["POST"])
def api_queue_prev():
    err = _require_queue()
    if err:
        return err
    state.queue.prev()
    return jsonify(state.queue.snapshot())


@app.route("/api/queue/stop", methods=["POST"])
def api_queue_stop():
    err = _require_queue()
    if err:
        return err
    state.queue.stop()
    return jsonify(state.queue.snapshot())


@app.route("/api/queue/clear", methods=["POST"])
def api_queue_clear():
    err = _require_queue()
    if err:
        return err
    state.queue.clear()
    return jsonify(state.queue.snapshot())


# ----------------------------------------------------------------------
# Playlists
# ----------------------------------------------------------------------

@app.route("/api/playlists")
def api_playlists_list():
    save_dir = os.path.expanduser("~/.playlists")
    if not os.path.isdir(save_dir):
        return jsonify({"playlists": []})
    names = [f[:-4] for f in os.listdir(save_dir) if f.endswith(".m3u")]
    return jsonify({"playlists": sorted(names)})


@app.route("/api/playlists/save", methods=["POST"])
def api_playlists_save():
    err = _require_queue()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    ok = state.queue.save_playlist(name)
    return jsonify({"ok": ok})


@app.route("/api/playlists/load", methods=["POST"])
def api_playlists_load():
    err = _require_queue()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    ok = state.queue.load_playlist(name)
    return jsonify({"ok": ok, **state.queue.snapshot()})


# ----------------------------------------------------------------------
# Logs & live stream
# ----------------------------------------------------------------------

@app.route("/api/logs")
def api_logs():
    return jsonify({"logs": list(LOG_BUFFER)})


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events stream: log lines, queue changes, renderer/server
    connectivity transitions, and once-a-second playback ticks."""
    q = events.subscribe()

    def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except Exception:
                    yield ": keepalive\n\n"
        finally:
            events.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream")


def create_app():
    return app


# ----------------------------------------------------------------------
# Startup: reconnect to the saved server/renderer and start the
# connectivity/position ticker.
#
# Deliberately at module level, NOT inside `if __name__ == "__main__":` --
# a WSGI server (waitress, gunicorn, etc.) imports this module and reads
# the `app` object directly without ever running this file as a script,
# so anything gated behind __main__ would silently never happen under
# systemd. Module-level code runs exactly once, the moment the module is
# first imported, which is what we want here either way.
# ----------------------------------------------------------------------
logger.info("Initializing DLNA Web Controller...")
state.startup()


def parse_args():
    parser = argparse.ArgumentParser(description="DLNA Web Controller (dev server)")
    parser.add_argument(
        "--port", type=int, default=None,
        help="Port to listen on (default: $DLNA_WEB_PORT or 5000)"
    )
    parser.add_argument(
        "--host", type=str, default=None,
        help="Interface to bind to (default: $DLNA_WEB_HOST or 0.0.0.0, i.e. all interfaces)"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    port = args.port if args.port is not None else int(os.environ.get("DLNA_WEB_PORT", 5000))
    host = args.host if args.host is not None else os.environ.get("DLNA_WEB_HOST", "0.0.0.0")

    logger.info(f"Starting Flask dev server on {host}:{port} ...")
    logger.info("(For a boot-time deployment, run this under waitress + systemd instead -- see README_WEB.md)")
    app.run(host=host, port=port, threaded=True, debug=False)

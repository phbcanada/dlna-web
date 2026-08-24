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
# --------------------------------------------------------------------------
# Web-service refactor notes:
#   * print()s replaced with logging (this module was already headless,
#     so this is a mechanical change).
#   * Every mutation (add/next/prev/clear/play/stop) now publishes a
#     "queue_status" event, and playback attempts publish "now_playing",
#     so a browser tab's queue panel updates live without polling.
#   * Added snapshot() (JSON-safe queue dump) and play_at(index) for the
#     web API -- clicking a track in the queue panel jumps straight to it.
#   * Renderer commands already return True/False (see dlnarenderer.py);
#     this module now surfaces failures as warnings + events instead of
#     silently trusting they worked, since the renderer being off is an
#     expected state, not a bug.
#
# Architecture refactor (queue/session split):
#   Historically this module had a single PlayQueue class that conflated
#   two different lifetimes: the track list + position (which has no
#   reason to depend on a renderer being attached) and the renderer
#   session (GENA/polling, transport commands -- which by definition
#   needs a live renderer). That coupling meant the queue was destroyed
#   and recreated every time a renderer was selected or lost, even though
#   nothing about "what tracks are queued" actually changed.
#
#   This is now two classes:
#     - Queue: pure data. Track list, current_idx, shuffle bookkeeping,
#       playlist save/load. Never touches a renderer. Long-lived --
#       created once and survives renderer selection/loss.
#     - PlaybackSession: renderer-bound. Owns the GENA listener/polling
#       thread and every call that actually pushes a URI to a renderer.
#       Takes a Queue reference and asks it "what's the next/prev/current
#       track" via advance()/retreat()/jump_to()/prepare_for_play(),
#       which return data (or None) without side effects on the
#       renderer -- only PlaybackSession decides whether to act on what
#       comes back.
#
#   PlayQueue below is now a thin backward-compatible facade over both,
#   kept so the original CLI (controller.py/main.py/dlnabrowser.py)
#   doesn't need to change at all.
# --------------------------------------------------------------------------
import threading
import time
import os
import logging
import random

import requests
import socket
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

from events import events

logger = logging.getLogger("playqueue")
gena_logger = logging.getLogger("playqueue.gena")


# This handles the inbound network traffic from the device
class GENAEventHTTPHandler(BaseHTTPRequestHandler):
    def do_NOTIFY(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length > 0:
            payload = self.rfile.read(content_length).decode('utf-8')
            if "LastChange" in payload:
                self.process_transport_event(payload)

        # Always return HTTP 200 OK to acknowledge the event receipt
        self.send_response(200)
        self.end_headers()

    def process_transport_event(self, xml_payload):
        try:
            if isinstance(xml_payload, bytes):
                xml_payload = xml_payload.decode('utf-8', errors='ignore')

            sanitized_xml = xml_payload.replace("& ", "&amp; ")

            root = ET.fromstring(sanitized_xml)
            for item in root.iter():
                if 'LastChange' in item.tag:
                    inner_xml = item.text
                    if not inner_xml:
                        continue

                    sanitized_inner = inner_xml.replace("& ", "&amp; ")
                    try:
                        inner_root = ET.fromstring(sanitized_inner)
                    except ET.ParseError:
                        import re
                        state_match = re.search(r'TransportState\s+val="([^"]+)"', inner_xml)
                        if state_match:
                            class DummyNode:
                                tag = 'TransportState'
                                attrib = {'val': state_match.group(1)}
                            inner_root = [DummyNode()]
                        else:
                            continue

                    for state_node in inner_root.iter():
                        if 'TransportState' in state_node.tag:
                            state = state_node.attrib.get('val', '')
                            # NOTE: attribute name kept as "play_queue" for
                            # minimal diff, but this is now a PlaybackSession
                            # instance (see _start_gena_listener below).
                            session = self.server.play_queue

                            with session.state_lock:
                                current_was_playing = session.was_playing

                                gena_logger.debug(
                                    f"Device broadcast: {state} (was_playing={current_was_playing})"
                                )

                                if state in ("PLAYING", "TRANSITIONING"):
                                    session.was_playing = True

                                elif state in ("STOPPED", "NO_MEDIA_PRESENT", "PAUSED_PLAYBACK"):
                                    if current_was_playing and state != "PAUSED_PLAYBACK":
                                        session.was_playing = False
                                        gena_logger.info("Track ended -- advancing queue.")
                                        session.next()

        except Exception as e:
            gena_logger.warning(f"GENA event parsing error: {e}")

    def log_message(self, format, *args):
        # Silence BaseHTTPRequestHandler's default access-log-to-stderr; we
        # log meaningfully above instead.
        pass


class Queue:
    """Renderer-independent play queue: track list, position, shuffle
    bookkeeping, and playlist file I/O. Nothing in this class calls a
    renderer or assumes one exists -- it's safe to build up, browse, and
    reorder a queue with no renderer selected at all.

    Mutation methods that affect *what track is current* (advance,
    retreat, jump_to, prepare_for_play) return the resulting track dict
    (or None) rather than playing it -- it's up to the caller (normally
    a PlaybackSession) to decide whether/how to push that to a renderer.
    This keeps "what's next" (a queue question) separate from "tell the
    device to play it" (a session question).
    """

    def __init__(self):
        self.queue = []            # List of dicts: [{'title': x, 'uri': y, 'mime': z}]
        self.current_idx = -1

        # RLock, not Lock: clear() calls stop-adjacent bookkeeping while
        # already holding the lock in some call paths below.
        self.lock = threading.RLock()

        # Shuffle: deliberately does NOT reorder self.queue itself -- the
        # displayed queue, playlist saves, and remove_at()'s index
        # bookkeeping all assume a stable list. Instead it only changes
        # what advance()/retreat() pick: shuffle_history tracks which
        # indices have already been "current" this shuffle session (in
        # the order they were played), and advance() randomly picks from
        # whatever's left. No auto-loop -- once everything's been
        # played, advance() reports "stop", same as linear playback
        # reaching the end of the queue.
        self.shuffle_enabled = False
        self.shuffle_history = []
        # True right after a full shuffle pass finishes (all tracks
        # played, history cleared) until the next navigation action.
        # prepare_for_play() consults this to start a genuinely fresh
        # shuffle pass (a new random pick) instead of just replaying
        # whatever track happened to be current when the pass ended.
        self.shuffle_exhausted = False

    # ------------------------------------------------------------------
    # Web-facing helpers
    # ------------------------------------------------------------------

    def snapshot(self):
        """JSON-safe dump of queue contents + position, for the REST API
        and for event payloads."""
        with self.lock:
            return {
                "queue": [
                    {"title": t.get("title"), "uri": t.get("uri")}
                    for t in self.queue
                ],
                "current_idx": self.current_idx,
                "shuffle": self.shuffle_enabled,
            }

    def _publish_queue_status(self):
        events.publish("queue_status", self.snapshot())

    def current_track(self):
        """Returns a shallow copy of the track at current_idx, or None if
        nothing's current. Used by state.py to overlay browse-time
        library metadata (e.g. artist) that a renderer's own
        GetPositionInfo/TrackMetaData echo doesn't reliably include --
        gmediarender in particular doesn't populate it, so the renderer
        isn't always the authoritative source for every display field
        the way it is for title/duration/position."""
        with self.lock:
            if 0 <= self.current_idx < len(self.queue):
                return dict(self.queue[self.current_idx])
        return None

    def get_current_track(self):
        """CLI-facing equivalent of current_track() (kept as a separate
        name for backward compatibility with controller.py)."""
        with self.lock:
            if 0 <= self.current_idx < len(self.queue):
                return self.queue[self.current_idx]
            return None

    def set_shuffle(self, enabled):
        """Turns shuffle on/off. Always starts a fresh shuffle_history --
        re-enabling shuffle later in the same session doesn't resume a
        stale partial history from before, it starts over, which matches
        how shuffle behaves in most media players."""
        with self.lock:
            self.shuffle_enabled = bool(enabled)
            self.shuffle_history = []
            self.shuffle_exhausted = False
            logger.info(f"Shuffle {'enabled' if self.shuffle_enabled else 'disabled'}.")
        self._publish_queue_status()

    def _mark_played_for_shuffle(self, idx):
        """Caller must hold self.lock. Records idx as "already played"
        this shuffle session -- called whenever current_idx is about to
        move away from it, whether via advance(), a manual jump_to(), or
        the natural end-of-track auto-advance (which also just calls
        advance()). Only meaningful while shuffle is on."""
        if self.shuffle_enabled and 0 <= idx < len(self.queue):
            self.shuffle_history.append(idx)

    def _reindex_shuffle_history_after_removal(self, removed_index):
        """Caller must hold self.lock. Keeps shuffle_history's positional
        indices valid after a track at removed_index is deleted --
        mirrors the same index-shifting current_idx already gets in
        remove_at(): an entry pointing at the removed track is dropped
        (it no longer exists to be "played" or "not played"), everything
        after it shifts down by one, entries before it are untouched."""
        self.shuffle_history = [
            (h - 1 if h > removed_index else h)
            for h in self.shuffle_history
            if h != removed_index
        ]

    def reset_position(self):
        """Resets playback position to the start of the queue without
        touching queue contents -- called by AppState whenever the
        renderer session is torn down (lost, or a different renderer
        selected). Deliberately mirrors "reached the end of the queue":
        nothing is playing, current_idx points at track 0 (or -1 if the
        queue is empty) ready for a future Play, and no attempt is made
        to reconcile this against whatever the newly-attached renderer
        might separately be reporting as playing. A future Play on a new
        session starts fresh from here rather than resuming whatever was
        "current" under the old renderer."""
        with self.lock:
            self.current_idx = 0 if self.queue else -1
            self.shuffle_history = []
            self.shuffle_exhausted = False
        self._publish_queue_status()

    def jump_to(self, index):
        """Moves current_idx directly to `index` -- used both by play_at
        (clicking a track in the queue panel) and internally. Does not
        play anything; returns the track at that position so the caller
        can decide whether to push it to a renderer.
        Returns {"ok": bool, "track": dict|None}."""
        with self.lock:
            self.shuffle_exhausted = False
            if not (0 <= index < len(self.queue)):
                return {"ok": False, "track": None}
            if index != self.current_idx:
                self._mark_played_for_shuffle(self.current_idx)
            self.current_idx = index
            track = dict(self.queue[self.current_idx])
        self._publish_queue_status()
        return {"ok": True, "track": track}

    def prepare_for_play(self):
        """Called when Play is pressed. Picks the track that should
        start playing: a fresh shuffle pick if the previous pass just
        finished, current_idx if one's already set, or track 0 if
        nothing's current yet. Returns the track dict, or None if the
        queue is empty."""
        with self.lock:
            if not self.queue:
                return None
            if self.shuffle_enabled and self.shuffle_exhausted:
                # A full shuffle pass just finished (see advance()).
                # Start a genuinely fresh pass -- a new random pick --
                # rather than just replaying whatever track happened to
                # be current when the pass ended.
                self.shuffle_exhausted = False
                self.current_idx = random.choice(range(len(self.queue)))
                logger.info("Shuffle: starting a fresh pass.")
            elif self.current_idx == -1:
                self.current_idx = 0
            track = dict(self.queue[self.current_idx])
        self._publish_queue_status()
        return track

    def advance(self):
        """Moves to the next track (linear or shuffle). Does not play
        anything -- returns a dict describing what happened:
          {"track": dict, "stop": False}       -- move to this track
          {"track": None, "stop": True}        -- end of queue, stop
          {"track": None, "stop": False, "no_op": True}
                                                 -- shuffle pass already
                                                    finished; caller
                                                    should do nothing
                                                    (only Play starts a
                                                    fresh pass)
        """
        with self.lock:
            if self.shuffle_enabled:
                if self.shuffle_exhausted:
                    # This pass already finished. Deliberately a no-op
                    # here -- only an explicit Play starts a fresh pass
                    # (see prepare_for_play()). If advance() also
                    # silently restarted it, repeatedly pressing Next
                    # would function as an implicit auto-loop, which is
                    # specifically not wanted.
                    logger.info("Shuffle: this pass is finished -- press Play to start a fresh one.")
                    self._publish_queue_status()
                    return {"track": None, "stop": False, "no_op": True}
                result = self._shuffle_advance()
            elif self.current_idx + 1 < len(self.queue):
                self.current_idx += 1
                result = {"track": dict(self.queue[self.current_idx]), "stop": False}
            else:
                logger.info("End of play queue reached.")
                result = {"track": None, "stop": True}
        self._publish_queue_status()
        return result

    def _shuffle_advance(self):
        """Caller must hold self.lock. Marks the current track as played,
        then randomly picks one of whatever's left. Once everything has
        been played, reports stop (no auto-loop, matching linear
        advance()'s end-of-queue behavior), clears shuffle_history, and
        sets shuffle_exhausted -- see prepare_for_play(), the only thing
        that clears that flag and starts a fresh pass."""
        self._mark_played_for_shuffle(self.current_idx)
        remaining = [i for i in range(len(self.queue)) if i not in self.shuffle_history]
        if not remaining:
            logger.info("Shuffle: all tracks in the queue have been played.")
            self.shuffle_history = []
            self.shuffle_exhausted = True
            return {"track": None, "stop": True}
        self.current_idx = random.choice(remaining)
        return {"track": dict(self.queue[self.current_idx]), "stop": False}

    def retreat(self):
        """Moves to the previous track (linear or shuffle). Does not play
        anything -- returns {"track": dict, "stop": False} or
        {"track": None, "stop": False, "no_op": True} when already at
        the first track / no shuffle history to go back to (deliberately
        never "stop"s -- there's nothing to stop, playback just doesn't
        move)."""
        with self.lock:
            if self.shuffle_enabled:
                if self.shuffle_history:
                    self.current_idx = self.shuffle_history.pop()
                    result = {"track": dict(self.queue[self.current_idx]), "stop": False}
                else:
                    # Empty history covers both "nothing played yet" and
                    # "a pass just finished" (shuffle_history is cleared
                    # on exhaustion) -- correctly a no-op either way, and
                    # deliberately doesn't touch shuffle_exhausted so a
                    # follow-up Play still starts a fresh pass correctly.
                    logger.info("No earlier shuffle history to go back to.")
                    result = {"track": None, "stop": False, "no_op": True}
            elif self.current_idx > 0:
                self.current_idx -= 1
                result = {"track": dict(self.queue[self.current_idx]), "stop": False}
            else:
                logger.info("Already at the first track.")
                result = {"track": None, "stop": False, "no_op": True}
        self._publish_queue_status()
        return result

    def clear(self):
        """Clears queue contents and resets position. Does NOT touch a
        renderer -- if a session is attached, the caller (PlaybackSession
        / the route) is responsible for stopping it first."""
        with self.lock:
            self.queue.clear()
            self.current_idx = -1
            self.shuffle_history = []
            self.shuffle_exhausted = False
            logger.info("Queue cleared.")
        self._publish_queue_status()

    def display_queue(self):
        """CLI-only pretty-printer."""
        with self.lock:
            print("\n" + "=" * 50)
            print(" 🎶 CURRENT PLAY QUEUE:")
            print("=" * 50)
            if not self.queue:
                print("   (Queue is empty)")
            else:
                for idx, track in enumerate(self.queue):
                    prefix = "➔ ▶ " if idx == self.current_idx else "    "
                    print(f"{prefix}{idx + 1}. {track['title']}")
            print("=" * 50)

    def add_to_queue(self, track_item):
        with self.lock:
            self.queue.append(track_item)
            logger.info(f"Queued: {track_item['title']}")
            if self.current_idx == -1:
                self.current_idx = 0
        self._publish_queue_status()

    def insert_and_select(self, track_item):
        """Inserts track_item immediately after the current track (or at
        the front if nothing's current) and makes it current. Used for
        "play this now" -- returns the track so the caller can decide
        whether to push it to a renderer immediately."""
        with self.lock:
            insert_pos = self.current_idx + 1 if self.current_idx != -1 else 0
            self.queue.insert(insert_pos, track_item)
            self.current_idx = insert_pos
            track = dict(self.queue[self.current_idx])
        self._publish_queue_status()
        return track

    def remove_at(self, index):
        """Removes the track at `index` from the queue -- used by the "X"
        button on each queue row. Does not itself talk to a renderer;
        returns enough information for a PlaybackSession (if attached)
        to decide what to do about playback:
          {"ok": True, "removed": dict, "advance_track": dict|None,
           "should_stop": bool}
        advance_track is set when the removed track was the currently-
        playing one and there's a track now in its place (the caller
        should push that to the renderer, same as Next); should_stop is
        set when the queue is now empty, or the removed track was the
        last (and current) one with nothing to advance to."""
        with self.lock:
            if not (0 <= index < len(self.queue)):
                return {"ok": False}

            removing_current = (index == self.current_idx)
            removed = self.queue.pop(index)
            logger.info(f"Removed from queue: {removed.get('title', 'Unknown')}")
            self._reindex_shuffle_history_after_removal(index)

            advance_track = None
            should_stop = False

            if not self.queue:
                self.current_idx = -1
                should_stop = True
            elif removing_current:
                if index < len(self.queue):
                    # The track that followed it has slid into this same
                    # position -- caller should play it, same as Next.
                    self.current_idx = index
                    advance_track = dict(self.queue[self.current_idx])
                else:
                    # Removed the last (and current) track -- nothing to
                    # advance to, so caller should stop, same as
                    # end-of-queue.
                    self.current_idx = len(self.queue) - 1
                    logger.info("Removed the last (and currently playing) track -- nothing left to advance to.")
                    should_stop = True
            elif index < self.current_idx:
                self.current_idx -= 1

        self._publish_queue_status()
        return {
            "ok": True,
            "removed": removed,
            "advance_track": advance_track,
            "should_stop": should_stop,
        }

    # ------------------------------------------------------------------
    # Playlist file I/O -- operates on disk + queue data only, no renderer
    # ------------------------------------------------------------------

    def save_playlist(self, playlist_name):
        """Generates a local server-compatible M3U file from the current queue tracks."""
        if not playlist_name:
            playlist_name = "my_playlist"

        save_dir = os.path.expanduser("~/.playlists")
        os.makedirs(save_dir, exist_ok=True)
        filepath = os.path.join(save_dir, f"{playlist_name}.m3u")

        try:
            with self.lock:
                tracks_snapshot = list(self.queue)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for track in tracks_snapshot:
                    rel_path = track.get('relative_path')
                    uri = track.get('uri')
                    if rel_path and uri:
                        f.write(f"#EXTINF:-1,{track['title']}\n")
                        f.write(f"#URI:{uri}\n")
                        f.write(f"{rel_path}\n")

            logger.info(f"Playlist '{playlist_name}' saved to {filepath}")
            return True
        except Exception as e:
            logger.warning(f"Failed to save playlist '{playlist_name}': {e}")
            return False

    def load_playlist(self, playlist_name):
        """Loads and appends tracks from a local M3U file back into the active play queue."""
        save_dir = os.path.expanduser("~/.playlists")
        filepath = os.path.join(save_dir, f"{playlist_name}.m3u")

        if not os.path.exists(filepath):
            logger.warning(f"Playlist file not found: {filepath}")
            return False

        try:
            new_tracks = []
            current_title = "Unknown Track"
            current_uri = None

            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#EXTM3U"):
                        continue

                    if line.startswith("#EXTINF:"):
                        parts = line.split(",", 1)
                        if len(parts) > 1:
                            current_title = parts[1]
                    elif line.startswith("#URI:"):
                        current_uri = line[5:].strip()
                    elif not line.startswith("#"):
                        uri = current_uri if current_uri else (line if line.startswith("http") else "")
                        if uri:
                            new_tracks.append({
                                'title': current_title,
                                'uri': uri,
                                'relative_path': line
                            })
                        current_title = "Unknown Track"
                        current_uri = None

            if new_tracks:
                with self.lock:
                    initial_empty = len(self.queue) == 0
                    self.queue.extend(new_tracks)
                    if initial_empty and self.current_idx == -1:
                        self.current_idx = 0
                logger.info(f"Loaded {len(new_tracks)} tracks from playlist '{playlist_name}'.")
                self._publish_queue_status()
                return True

            logger.warning(f"No valid streaming tracks found in playlist '{playlist_name}'.")
            return False
        except Exception as e:
            logger.warning(f"Failed to load playlist '{playlist_name}': {e}")
            return False

    def add_track_to_playlist(self, index, playlist_name):
        """Appends the single track at queue position `index` to a
        playlist file on disk -- unlike save_playlist/load_playlist,
        which operate on the *entire* queue, this touches one track and
        leaves the queue itself untouched. Backs the "+" button on each
        queue row, for building up a playlist gradually while browsing
        and queueing normally.

        Skips (without treating it as an error) if a track with the same
        URI is already in the playlist file -- clicking "+" again on a
        track already added is far more likely an accidental repeat
        click than a deliberate request for a duplicate entry.

        Returns {"ok": bool, "added": bool, "title": str|None}."""
        with self.lock:
            if not (0 <= index < len(self.queue)):
                return {"ok": False, "added": False, "title": None}
            track = dict(self.queue[index])  # snapshot while holding the lock

        title = track.get("title", "Unknown")
        rel_path = track.get("relative_path")
        uri = track.get("uri")
        if not rel_path or not uri:
            logger.warning(f"Cannot add '{title}' to playlist '{playlist_name}' -- missing library path.")
            return {"ok": False, "added": False, "title": title}

        save_dir = os.path.expanduser("~/.playlists")
        os.makedirs(save_dir, exist_ok=True)
        filepath = os.path.join(save_dir, f"{playlist_name}.m3u")

        try:
            existing_uris = set()
            file_exists = os.path.exists(filepath)
            if file_exists:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("#URI:"):
                            existing_uris.add(line[5:].strip())

            if uri in existing_uris:
                logger.info(f"'{title}' is already in playlist '{playlist_name}' -- skipped duplicate.")
                return {"ok": True, "added": False, "title": title}

            with open(filepath, "a", encoding="utf-8") as f:
                if not file_exists:
                    f.write("#EXTM3U\n")
                f.write(f"#EXTINF:-1,{title}\n")
                f.write(f"#URI:{uri}\n")
                f.write(f"{rel_path}\n")

            logger.info(f"Added '{title}' to playlist '{playlist_name}'.")
            return {"ok": True, "added": True, "title": title}
        except Exception as e:
            logger.warning(f"Failed to add '{title}' to playlist '{playlist_name}': {e}")
            return {"ok": False, "added": False, "title": title}


class PlaybackSession:
    """Renderer-bound playback session: GENA/polling monitoring and every
    call that actually pushes a URI to a renderer. Takes a Queue and asks
    it what track should be current via advance()/retreat()/jump_to()/
    prepare_for_play() -- those calls never touch the renderer
    themselves, so this class is the only place that decides whether/how
    to act on the result.

    Never raises on renderer failure -- a rejected or unreachable
    renderer is expected, not exceptional; methods return True/False and
    log/publish instead."""

    def __init__(self, renderer, queue):
        self.renderer = renderer
        self.queue = queue

        self.running = True
        self.state_lock = threading.Lock()
        self.was_playing = False

        # =====================================================================
        # EXPERIMENTAL TOGGLE: Set to True to override monitor loop with GENA
        # =====================================================================
        self.use_gena = True

        # Reference to the running GENA HTTPServer, if any, so shutdown()
        # can actually stop it and release its port -- without this, the
        # listener from a previous renderer selection stays bound forever,
        # and every renderer switch after the first one silently loses
        # GENA (falls back to polling) because the port is still taken.
        self.gena_server = None

    # ------------------------------------------------------------------
    # Playback -- reads queue state via Queue's data methods, then acts
    # on the renderer based on what comes back
    # ------------------------------------------------------------------

    def _start_playback(self, track):
        """Pushes `track` to the renderer and publishes now_playing.
        Caller (play/play_now/next/prev/play_at/remove_at) is
        responsible for getting the track from the Queue first."""
        self.was_playing = False
        ok = self.renderer.play_uri(track['uri'], track['title'])
        if ok:
            self.was_playing = True
            logger.info(f"Now playing: {track['title']}")
        else:
            logger.warning(
                f"Could not start playback for '{track['title']}' -- "
                f"renderer '{self.renderer.friendly_name}' may be offline."
            )
        events.publish("now_playing", {"title": track['title'], "ok": ok})

    def play(self):
        track = self.queue.prepare_for_play()
        if track is None:
            logger.info("Play requested but queue is empty.")
            return
        self._start_playback(track)

    def play_now(self, track_item):
        track = self.queue.insert_and_select(track_item)
        self._start_playback(track)

    def pause(self):
        ok = self.renderer.pause()
        if not ok:
            logger.warning("Pause command failed -- renderer may be offline.")
        return ok

    def toggle_play(self):
        """Toggles between play and pause depending on the renderer's active state."""
        try:
            state = self.renderer.get_transport_state()
        except Exception:
            state = "STOPPED"

        if state in ("PLAYING", "TRANSITIONING"):
            logger.info("Toggling: pausing playback.")
            self.pause()
        elif state == "UNKNOWN":
            logger.warning("Cannot toggle playback -- renderer is not responding.")
        else:
            logger.info("Toggling: resuming/starting playback.")
            self.play()

    def stop(self):
        self.was_playing = False
        ok = self.renderer.stop()
        if not ok:
            logger.debug("Stop command did not reach renderer (likely already offline).")
        return ok

    def next(self):
        result = self.queue.advance()
        if result.get("no_op"):
            return
        if result["track"] is not None:
            self._start_playback(result["track"])
        elif result["stop"]:
            self.stop()

    def prev(self):
        result = self.queue.retreat()
        if result.get("no_op"):
            return
        if result["track"] is not None:
            self._start_playback(result["track"])

    def play_at(self, index):
        """Jumps directly to a specific queue position and plays it --
        used when the user clicks a track in the queue panel."""
        result = self.queue.jump_to(index)
        if not result["ok"]:
            return False
        self._start_playback(result["track"])
        return True

    def remove_at(self, index):
        """Removes a track from the queue and, if that affects what's
        currently playing, tells the renderer accordingly (advance to
        the track that slid into its place, or stop if there's nothing
        left)."""
        result = self.queue.remove_at(index)
        if not result["ok"]:
            return False
        if result["should_stop"]:
            self.stop()
        elif result["advance_track"] is not None:
            self._start_playback(result["advance_track"])
        return True

    # ------------------------------------------------------------------
    # GENA subscription plumbing
    # ------------------------------------------------------------------

    def _get_local_ip(self):
        """Forces finding the real local IP interface facing the renderer."""
        try:
            from urllib.parse import urlparse

            event_url = getattr(self.renderer, 'avtransport_event_url', "")
            if event_url:
                target_host = urlparse(event_url).hostname
            else:
                target_host = self.renderer.host

            if not target_host or target_host.lower() == "unknown":
                return "0.0.0.0"

            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target_host, 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception as e:
            logger.debug(f"Auto-IP detection failed: {e}")
            return "0.0.0.0"

    def _start_gena_listener(self):
        """Spins up the HTTP backend and sends the SUBSCRIBE packet safely."""
        local_ip = self._get_local_ip()
        local_port = 8089  # Choose any open port on your system

        logger.info(f"Local GENA listener binding to: http://{local_ip}:{local_port}")
        if local_ip in ["0.0.0.0", "127.0.0.1"]:
            logger.warning("Local IP resolved to loopback -- renderer won't be able to route events back here.")

        def run_server():
            try:
                server = HTTPServer((local_ip, local_port), GENAEventHTTPHandler)
                server.play_queue = self
                self.gena_server = server
                server.serve_forever()
            except Exception as e:
                logger.warning(f"GENA background server failed to start: {e}. Falling back to polling.")
                self._start_local_monitor_thread()

        threading.Thread(target=run_server, daemon=True).start()

        def send_subscribe():
            event_url = getattr(self.renderer, 'avtransport_event_url', None)
            if not event_url:
                self._start_local_monitor_thread()
                return

            headers = {
                "HOST": event_url.split("://")[1].split("/")[0],
                "TIMEOUT": "Second-300"
            }

            current_sid = getattr(self, 'gena_sid', None)

            if current_sid:
                headers["SID"] = current_sid
                logger.debug(f"Renewing GENA subscription lease for SID: {current_sid}")
            else:
                headers["CALLBACK"] = f"<http://{local_ip}:{local_port}/>"
                headers["NT"] = "upnp:event"
                logger.info(f"Sending initial SUBSCRIBE to: {event_url}")

            try:
                res = requests.request("SUBSCRIBE", event_url, headers=headers, timeout=5)

                if res.status_code == 200:
                    if not current_sid:
                        self.gena_sid = res.headers.get('SID')
                        logger.info(f"GENA subscription active. SID: {self.gena_sid}")
                    else:
                        logger.debug("GENA subscription lease renewed.")

                    if getattr(self, 'use_gena', True):
                        self.renewal_timer = threading.Timer(150.0, send_subscribe)
                        self.renewal_timer.daemon = True
                        self.renewal_timer.start()
                else:
                    logger.warning(f"GENA subscribe rejected ({res.status_code}). Falling back to polling.")
                    self._start_local_monitor_thread()

            except Exception as e:
                logger.info(f"GENA handshake failed ({e}) -- renderer likely offline or doesn't support "
                            f"eventing. Falling back to polling.")
                self._start_local_monitor_thread()

        # A short delay guarantees the HTTP server thread above is fully
        # listening before we try to subscribe.
        threading.Timer(1.0, send_subscribe).start()

    def _start_local_monitor_thread(self):
        """Background SOAP-polling fallback for renderers that reject GENA."""
        logger.info("Launching background polling monitor thread.")
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def _monitor_loop(self):
        """Background thread loop verifying track status every second.
        Tolerant of a renderer that is intermittently or permanently
        unreachable -- it just keeps polling quietly rather than crashing
        the thread, since "renderer is off right now" is a normal state
        for a device that isn't always on."""
        while self.running:
            try:
                if self.renderer.control_url:
                    state = self.renderer.get_transport_state()

                    if state in ("PLAYING", "TRANSITIONING"):
                        self.was_playing = True
                    elif state in ("STOPPED", "NO_MEDIA_PRESENT", "PAUSED_PLAYBACK"):
                        if self.was_playing and state != "PAUSED_PLAYBACK":
                            self.was_playing = False
                            self.next()
                    # state == "UNKNOWN" (renderer unreachable): nothing to
                    # do here: AppState's ticker thread is responsible for
                    # surfacing connectivity to the UI. We simply don't
                    # advance the queue on an indeterminate state.
            except Exception as e:
                logger.debug(f"Monitor loop iteration error: {e}")
            time.sleep(1.5)

    def start_monitoring(self):
        if self.use_gena:
            self._start_gena_listener()
        else:
            self._start_local_monitor_thread()

    def shutdown(self):
        """Cleans up background assets on application close or renderer switch."""
        logger.info("Tearing down PlaybackSession resources.")
        if hasattr(self, 'renewal_timer'):
            self.renewal_timer.cancel()

        self.running = False

        if self.gena_server is not None:
            try:
                # shutdown() blocks until serve_forever() (running on the
                # listener's own thread) has actually exited, then
                # server_close() releases the socket -- by the time this
                # returns, the port is genuinely free for the next
                # session's GENA listener to bind to. Must be called
                # from a different thread than serve_forever() is
                # running on, which is always true here (this runs on
                # whatever thread triggered the renderer switch).
                self.gena_server.shutdown()
                self.gena_server.server_close()
                logger.info("GENA listener stopped and port released.")
            except Exception as e:
                logger.warning(f"Error stopping GENA listener (port may remain briefly held): {e}")
            finally:
                self.gena_server = None


class PlayQueue:
    """Backward-compatible facade combining a Queue + PlaybackSession
    under the original combined API, so the CLI (controller.py,
    main.py, dlnabrowser.py) doesn't need any changes. The web app
    (state.py/app.py) uses Queue and PlaybackSession directly instead,
    since it needs them decoupled."""

    def __init__(self, renderer, *args, **kwargs):
        self._queue = Queue()
        self._session = PlaybackSession(renderer, self._queue)

    @property
    def renderer(self):
        return self._session.renderer

    def start_monitoring(self):
        self._session.start_monitoring()

    def get_current_track(self):
        return self._queue.get_current_track()

    def display_queue(self):
        self._queue.display_queue()

    def add_to_queue(self, track_item):
        self._queue.add_to_queue(track_item)

    def next(self):
        self._session.next()

    def prev(self):
        self._session.prev()

    def toggle_play(self):
        self._session.toggle_play()

    def stop(self):
        self._session.stop()

    def clear(self):
        self._session.stop()
        self._queue.clear()

    def save_playlist(self, playlist_name):
        return self._queue.save_playlist(playlist_name)

    def load_playlist(self, playlist_name):
        return self._queue.load_playlist(playlist_name)

    def shutdown(self):
        self._session.shutdown()

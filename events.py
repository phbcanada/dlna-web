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
# events.py
#
# A minimal thread-safe publish/subscribe bus. Any part of the backend
# (the play queue, the renderer, the app state manager, the logging
# handler) can publish an event; the /api/stream SSE endpoint hands each
# connected browser tab its own subscriber queue and relays events as
# they happen.
#
# Kept deliberately dependency-free (no Flask-SocketIO / eventlet) so the
# whole project installs cleanly on a Raspberry Pi with just Flask + requests.
import threading
import queue
import time


class EventBus:
    def __init__(self, max_queue=200):
        self._subscribers = []
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self):
        """Registers a new subscriber and returns its private queue."""
        q = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event_type, data=None):
        """Fans an event out to every connected subscriber. Never blocks the
        caller: if a subscriber's queue is full (a slow/stuck browser tab),
        we drop that subscriber's oldest pending event rather than stall
        the publisher (which is often a background playback thread)."""
        payload = {"type": event_type, "data": data if data is not None else {}, "ts": time.time()}
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass


# Single process-wide bus, shared by every module.
events = EventBus()

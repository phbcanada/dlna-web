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
# logging_setup.py
#
# Every module in this project logs through the standard `logging` module
# instead of print(). That gets us three things for free:
#   1. Timestamps and log levels on everything (console output)
#   2. A rolling in-memory history (LOG_BUFFER) an /api/logs call can hand
#      to a freshly-loaded browser tab
#   3. Live tailing: each log record is also published on the event bus,
#      so a connected browser's log panel updates in real time via SSE.
import logging
import os
import collections

from events import events

LOG_BUFFER = collections.deque(maxlen=500)

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"


class BroadcastHandler(logging.Handler):
    """Mirrors every log record into LOG_BUFFER and onto the event bus."""

    def emit(self, record):
        try:
            entry = {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "ts": record.created,
            }
            LOG_BUFFER.append(entry)
            events.publish("log", entry)
        except Exception:
            # Logging must never be the thing that crashes the app.
            self.handleError(record)


def setup_logging():
    """Configures root logging once at process startup. Level is controlled
    by the DLNA_LOG_LEVEL env var (default INFO) so you can run
    `DLNA_LOG_LEVEL=DEBUG python app.py` on the Pi when troubleshooting a
    flaky renderer without editing code."""
    level_name = os.environ.get("DLNA_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid stacking duplicate handlers if setup_logging() is called twice
    # (e.g. under a reloader).
    if any(isinstance(h, BroadcastHandler) for h in root.handlers):
        return root

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    broadcast = BroadcastHandler()
    broadcast.setFormatter(formatter)
    root.addHandler(broadcast)

    # requests/urllib3 are chatty at DEBUG; keep them at WARNING regardless
    # of our own level so the log panel stays about *this* app.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return root

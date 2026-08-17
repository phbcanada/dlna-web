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
# config.py
#
# Tiny JSON-backed settings store. The media server is now a fixed,
# deploy-time value (MEDIA_SERVER_DESC_URL env var, read directly in
# state.py) rather than something picked at runtime, so the only thing
# left worth remembering here is which renderer to reconnect to on the
# next boot.
import json
import os
import logging

logger = logging.getLogger("config")

CONFIG_PATH = os.environ.get(
    "DLNA_WEB_CONFIG", os.path.expanduser("~/.dlna_web_config.json")
)

DEFAULTS = {
    "renderer_desc_url": None,
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(DEFAULTS)
            merged.update(data)
            return merged
        except Exception as e:
            logger.warning(f"Could not read config at {CONFIG_PATH}: {e}. Using defaults.")
    return dict(DEFAULTS)


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True
    except Exception as e:
        logger.warning(f"Could not write config to {CONFIG_PATH}: {e}")
        return False
